// Evidence adapter only: imports AP's real classifier/cursor functions without duplicating them.
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { expect, it } from "vitest";
import { classifyPane } from "ap-replay/nudge";
import { placedCursorY, screenToCursor, type PaneLook } from "ap-replay/tmux";

it("records actual AP verdicts and cursor-bounded busy hashes", () => {
  const indexPath = process.env.AP_CAPTURE_INDEX!;
  const directory = dirname(indexPath);
  const index = JSON.parse(readFileSync(indexPath, "utf8"));
  const look = (state: string): PaneLook => {
    const [y, x, , width, flag] = readFileSync(join(directory, state, "look.txt"), "utf8").trim().split(/\s+/).map(Number);
    return { cursorY: y, cursorX: x, paneWidth: width, cursorFlag: flag === 0 || flag === 1 ? flag : -1,
      rows: readFileSync(join(directory, state, "screen.txt"), "utf8").split("\n") };
  };
  const captures = index.fixtures.map((row: { id: string; expected: string }) => {
    const input = look(row.id);
    const actual = classifyPane(input.rows.join("\n"), "codex", placedCursorY(input));
    expect(actual).toBe(row.expected);
    return { state: row.id, expected: row.expected, actual, passed: actual === row.expected,
      placed_cursor_y: placedCursorY(input) };
  });
  const sha = (text: string) => createHash("sha256").update(text).digest("hex");
  const busy_pairs = ["100x40", "80-split"].map((size) => {
    const before = screenToCursor(look(`high-${size}-busy-midturn-t0`));
    const after = screenToCursor(look(`high-${size}-busy-midturn-t5`));
    expect(before).not.toBe(after);
    expect(sha(before)).not.toBe(sha(after));
    return { size, before_state: `high-${size}-busy-midturn-t0`, after_state: `high-${size}-busy-midturn-t5`,
      before_sha256: sha(before), after_sha256: sha(after), changed: before !== after, passed: true,
      producer: "actual AP screenToCursor export" };
  });
  expect(captures).toHaveLength(20);
  writeFileSync(process.env.AP_REPLAY_OBSERVER_OUTPUT!, JSON.stringify({ passed: true, captures, busy_pairs }, null, 2) + "\n");
});
