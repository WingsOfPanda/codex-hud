# Morandi Cool

Choose `morandi` in `/theme`, preview it, then press Enter to save. Escape cancels
the preview. The existing `tui.theme` setting stores the choice; existing bundled
and custom themes remain available.

Morandi Cool uses muted slate, blue-gray, sage, teal, ochre and clay. It covers
syntax and Markdown as well as Codex's controls, composer, menus, selections,
status lines, worker and job rows, notices and transcript output. Warnings and
errors retain distinct semantic colors and text. The optional `compact` status
item uses a cool gray-teal instead of pink, including with other themes.

The brighter palette keeps the same `morandi` choice and background. Main text
and secondary accents are lighter, and successful command dots share the Context
item's sage green. Failed commands remain red; attention colors remain amber.
The running-command activity animation is unchanged.

| Role | Authored color |
| --- | --- |
| Main text | `#E0E5E8` |
| Success and Context | `#A8C9AD` |
| Blue | `#A8C2D8` |
| Cyan and compact indicator | `#A2C6BE` |
| Failure | `#D09A8C` |
| Attention | `#CDBB86` |

The theme adapts colors as Codex paints its own terminal cells and scrollback.
It preserves the terminal's default background and does not change the terminal
emulator palette or external applications. Text, commands, tool results and
model requests are unchanged. Colored command output displayed inside Codex is
also visually muted; its stored bytes remain intact.

Truecolor gives the closest palette. ANSI256 uses representable colors with
contrast correction against known backgrounds. ANSI16 retains terminal-owned
semantic colors and selection cues, so its appearance depends on the terminal's
palette. `NO_COLOR` and disabled color output remain monochrome. Decorative
gradients retain brightness variation; informative text and painted surfaces
use the existing contrast rules.

For Morandi in ANSI256, contrast correction prefers a readable color from the
same hue family, keeping pastels from collapsing into identical grays on a light
background. If no matching hue is readable, contrast takes priority. Other themes
keep their existing color selection.

This feature is a development candidate until a release containing it is
installed. Editing source does not change an already running TUI or fleet config.

[Development verification](evidence/morandi-theme.json) records 483 native tests,
six picker lifecycle stages, nine command-display cases, six compact color modes
and all 23 terminal smoke stages. It distinguishes the final test-only corrections
from the unchanged compiled client and does not claim ARM release qualification.

[Brighter-palette verification](evidence/morandi-bright.json) separately records
137 focused native tests, seven terminal color-mode checks, six picker stages,
all nine command-display cases and 23 terminal smoke stages. Actual successful
command dots and Context emit the same green in both TrueColor and ANSI256.
This development evidence does not install or release the brighter palette.
