# Advisor workflow

Usage:

```text
codex-plus advisor --brief TEXT [--provider claude|astra] [--host local|SSH_ALIAS]
                   [--remote-command PATH] [--context-file PATH ...]
codex-plus advisor --help
```

Read this workflow before starting, resuming, or recovering a consultation.
`codex-plus advisor --help` prints this document from the executable itself;
the workflow is owned by Codex Plus and does not depend on its install path.

## Default: Claude Opus 5.5 / xhigh

```sh
codex-plus advisor --brief "Decision, constraints, evidence, and review focus"
codex-plus advisor --brief "Review these changes" --context-file src/example.rs
```

The default model is exactly `claude-opus-5-5`, with effort `xhigh`.
The central execution host is **xjp_l20_2**, addressed by SSH alias
`xjp-l20-2`. On that machine the helper runs Claude locally; elsewhere it
uses noninteractive SSH with host-key verification. Passwordless SSH and a
compatible Codex Plus helper on the central host are prerequisites.
For default routing, Claude Code and its authentication are needed only on the central host.
No persistent advisor service or daemon modification is needed.

Each request starts a fresh Claude Code process with tools, customizations,
MCP servers, and session persistence disabled. It receives the explicit brief,
bounded git metadata, and only the files supplied with `--context-file`.
It cannot inspect other repository files, run shell commands, or edit code.
Attach the evidence needed for the question; explain missing context rather
than interpreting an incomplete review as approval.

Repeat `--context-file PATH` for up to eight regular UTF-8 files, at most
128 KiB each and 512 KiB combined. Symlinks and nonregular files are refused.
The request carries filenames, content hashes, and contents; filenames are
data, never shell commands. Uncommitted file contents are included only through
explicitly attached files or a supplied diff file. Select files without
credentials or unrelated private data.

Prepare self-contained engineering briefs. Omit hardware or vendor names when
irrelevant to the question; preserve architecture, compatibility constraints,
and other facts that affect correctness. Generalization must preserve the
request's meaning and must not disguise a refused request.

### Routing and failures

```sh
codex-plus advisor --host local --brief "Local consultation"
codex-plus advisor --host xjp-l20-2 --brief "Explicit SSH consultation"
codex-plus advisor --host xjp-l20-2 --remote-command /path/to/codex-plus --brief "Candidate check"
```

`--remote-command` is a single executable path, not a shell expression.
Normal installed use needs no override. The internal `--remote-worker` protocol
is for the SSH transport, not a user-facing consultation command.
An explicit `--host local` always uses the caller's machine and therefore
requires Claude Code and authentication there. An explicit SSH alias always
uses SSH, including a connection back to the central machine itself.

The caller verifies its root session with its local official daemon.
The remote worker runs under the existing SSH account; it does not look up
the caller's thread in the central daemon. Request identity and an
architecture-independent worker implementation identity bind the response.
ARM callers and x86 workers may interoperate when their worker code matches.
An incompatible helper, wrong returned model, provider failure, invalid
structured result, or transport failure is an error; there is no automatic
fallback to another model or automatic retry.

For a completed `model_mismatch`, the main root may make **one rephrased retry
per consultation question** within the existing authorization. First confirm the
caller and remote worker have ended, then inspect the private log's
`advisor model metadata:` diagnostic. Missing or malformed metadata calls for
diagnosis; it is not evidence that a different model answered.

When the metadata identifies a different returned model, restate the question
more clearly while preserving its intent, evidence, technical constraints, and
context files. Keep the requested model and effort unchanged. Rephrasing must not
disguise a refused request. Record both attempt log paths and the wording change,
then launch one new consultation using the foreground workflow below. Accept only
a validated result. If that attempt fails, stop and report both errors; further
retries need new direction. An uncertain transport or cleanup outcome must be
resolved before any retry. This is a main-session decision, not a helper retry
loop, and a successful retry does not establish why the first call mismatched.

The caller and Claude worker each enforce a 25-minute deadline; the worker
also limits initial request reading to 30 seconds and monitors an open request channel.
Disconnect or cancellation terminates its owned Claude process and descendants.
A network partition can delay disconnect detection until SSH keepalive or the
worker deadline; report uncertain outcomes before considering a new request.
The SSH client uses a five-second keepalive interval and two unanswered probes.
The per-root lock excludes simultaneous callers on the originating machine;
it does not prove a remote process has stopped after a transport failure.
Do not start a replacement consultation while that remote outcome is unknown.
Independent roots use separate processes and status files.

## Explicit Astra compatibility

The previous local Codex-based advisor remains available when explicitly selected:

```sh
codex-plus advisor --provider astra --brief "Question, facts, and review focus"
```

Astra uses the newest visible numeric Astra model with `xhigh`, the native
Codex read-only sandbox, and local repository inspection. Host and context-file
options belong to the Claude route. The remaining lifecycle and review
instructions apply to both providers unless explicitly labeled Astra.

## Starting and reviewing a consultation

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
The git pointer is captured at the start. Claude receives explicit file contents
captured for this request. Astra's later inspection reads the live repository,
which may change during the consultation.

From Plus 0.3 the launcher uses the native Rust helper. Development requires
`./scripts/build-runtime.sh`; installed consultations require no Python. The
Astra provider launches the package's native Codex in its read-only sandbox.

Keep a consultation **foreground by default** when its result is needed for the
next decision. Mark its managed shell command `foreground` and keep the root turn
open in one bounded Code Mode cell until the result or the helper's 25-minute
deadline. A pending tool call makes no interim model requests. The marker only
controls display; the local wait below keeps the consultation pending. The
advisor footer continues to show progress. To cancel the consultation, stop its
native terminal (`/stop`); its detached guard stops and reaps owned descendants.
For SSH work, the remote worker performs cleanup when it detects disconnection.
An interrupted turn alone does not establish that its command stopped:
inspect the native shell's completion and private log, and preserve an unknown
remote outcome until cleanup is verified. Do not blindly rerun an interrupted call.

The conversation shows the active consultation locally:

```text
• Advisor on wake and cancellation fixes · 5:01
```

The topic summarizes what is being reviewed. The model and effort stay in the
footer. Completion changes the same row to `• Advised on … · M:SS`, or
`• Advisor failed on … · M:SS`; the main session then reviews the findings.
These are display rows, not model messages. The footer retains its model, effort,
call count and timing within the activity strip.

Give a foreground consultation a short topic in its command header:

```sh
# codex-plus-shell: foreground
# codex-plus-advisor: wake and cancellation fixes
codex-plus run -- codex-plus advisor --brief 'Review cancellation ownership in the attached workflow.' --context-file docs/advisor.md
```

The calling agent supplies this summary; it needs no extra model call. The
topic header also identifies an opaque wrapper such as `python3 consult.py`,
when that execution owns the consultation and returns its advisor JSON and exit
status. It must follow the foreground directive directly. Unmarked direct
advisor commands use a compact summary of their brief; unrelated shell jobs
retain their ordinary command headers. Background/auto commands keep the shell
workflow rather than the foreground advisor conversation row.

Use this pattern with a self-contained brief. The 26-minute local budget and
27-minute outer yield leave margin for the helper's deadline and cleanup:

```js
// @exec: {"yield_time_ms": 1620000, "max_output_tokens": 2000}
const deadline = Date.now() + 1560000;
let result = await tools.exec_command({
  cmd: "# codex-plus-shell: foreground\n# codex-plus-advisor: proposed approach\ncodex-plus run -- codex-plus advisor --brief 'Question, facts, constraints, and review focus'",
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

Only a daemon-confirmed root session with `CODEX_THREAD_ID` can start a
consultation through `--brief`. The internal worker trusts the authenticated
SSH account and does not require a central daemon thread.
The explicit Astra provider selects the newest visible numeric `gpt-<version>-astra` model advertising
`xhigh` through stock `model/list`; absence fails visibly. There is no model or
effort override. Each call uses a separate ephemeral native `exec`, ignores
user config and execpolicy rules, and uses the read-only sandbox. Explicit
`agents.enabled=false` and `features.multi_agent_v2=false` overrides disable
delegation even when the model catalog advertises the newer agent runtime.
It can inspect repository files through shell/git. It has a 25-minute
deadline; timeout or signals terminate its owned process group.

A detached native guard owns the consultation and each Git metadata probe. Its
owner lifeline closes even when `/stop` kills the terminal group immediately;
the guard then stops and reaps its children, including adopted descendants that
created their own sessions. This uses Linux subreaper support and `/proc`.
Metadata probes disable repository-configured fsmonitor hooks locally.
Result files have a separate 512 KiB transport limit; schema limits still count
characters. Invalid, oversized, symlinked, or nonregular result files fail visibly.

The footer reads private per-root status, including attempt count, timing, and
stable error code. It does not trigger model calls. The caller holds a per-root
lock while a consultation runs. Heartbeats update within five seconds; process death can leave a
stale running record. The next explicit call acquires the lock and replaces that
record without rerunning the old work. Status contains no brief or results.
On the caller, private logs retain the brief, explicit file contents, validated
result, and worker stderr under `$CODEX_HOME/codex-plus/logs`; these logs are not
automatically deleted. The central worker uses temporary prompt/output files
and removes them when it exits. Claude's own account/cache behavior is separate.

When model validation fails, the caller's private log also contains an
`advisor model metadata:` JSON line with the expected model, returned model names,
and canonical-model metadata. This diagnostic is bounded and excludes the prompt,
answer, and usage counters. It is available for both local and SSH consultations;
the error remains `model_mismatch`, with no automatic fallback or retry.

This repository supplies implementation and verification. Fleet-config owns the
installed helper wrapper and global instructions. The wrapper must route
`advisor` to this package's helper before installed use.

Global instruction pointer:

```markdown
## Advisor

Before starting, resuming, or recovering an advisor consultation, run
`codex-plus advisor --help` and follow its workflow.
The main root session owns consultations and reviews their findings before
acting. Advice grants no additional permission.
```
