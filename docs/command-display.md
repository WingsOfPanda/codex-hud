# Command display

Agent shell commands retain their recognizable `Running`, `Ran`, or `Failed`
header in the main transcript. The default preview occupies one visual line;
long arguments and multiline scripts are clipped with an ellipsis. Successful
output is folded, including JSON, logging receipts, and short command output.
Shell-based `Explored` activities retain their existing grouping in compact form.

`Show details` reveals the retained command and output. `Show less` folds it
again. Expansion stays attached to the activity while output arrives and when
the command completes. A restarted session starts with activities collapsed.
These are local display choices: execution, tool results supplied to the model,
context handling, and logging do not change.

A failed command keeps its failure status and exit code visible, with a short,
clipped output excerpt when available. An excerpt is evidence, not a diagnosis;
the main agent explains the cause after reviewing the result. Approval requests,
questions, and interactive input remain visible. Manual user-shell sessions,
special advisor rows, other tools, and the footer keep their existing behavior.
Plain or accessibility renderers without local disclosure retain their readable
output rather than hiding details that cannot be expanded.

## Optional status-line indicator

Select **Compact display** in `/statusline` to show `compact` on the first row.
Its configuration item is `compact-display` in the existing `tui.status_line`
list. Place it after `git-branch` to show it before the appended agent label.
The item reports the display mode; selecting or hiding it does not change
command folding. It is optional and does not rewrite existing selections.

With status-line colors enabled, the indicator uses cool gray-teal, distinct from the
model, directory, branch, usage, and reset colors. Color-disabled terminals keep
the text readable in monochrome. The Morandi theme supplies its own coordinated
status palette through the same item selection.

## Explain related commands together

Before a logical group of commands, the main agent briefly states its purpose.
After reviewing the results, it gives one combined outcome and the next step.
Consecutive commands with the same purpose do not each need a separate paragraph.
Change the explanation when the task changes or a failure needs attention.
This narration uses the existing conversation; display summaries make no extra
model calls and do not merge otherwise separate command activities.

For example, validating inputs, preparing a job, launching it, saving its
observation state, and submitting a Goal checkpoint can be explained together:

> Starting the corrected build in the background. These commands check the
> inputs, prepare and launch the job, save its tracking information, and submit
> a Goal wait checkpoint.

The outcome must distinguish launch from workload success, and a submitted
checkpoint from its finalization after the turn ends. State only what the
command receipts and observed results establish.
