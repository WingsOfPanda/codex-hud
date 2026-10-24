"""Run the shipped disclosure fixture and record actual Morandi completion marker colors."""
import argparse
import json
import os
from pathlib import Path
import sys


def marker(terminal, title, expected):
    matches = [(y, row.index('•')) for y, row in enumerate(terminal.screen.display)
        if '• ' + title in row]
    assert len(matches) == 1, (title, matches, terminal.text)
    y, x = matches[0]
    cell = terminal.screen.buffer[y][x]
    assert cell.fg == expected, (title, cell.fg, expected)
    return dict(title=title, foreground=cell.fg, bold=cell.bold)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'binary', 'launcher', 'evidence', 'short-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--theme')
    args = parser.parse_args()
    sys.path.insert(0, str(args.source / 'tests'))
    import tempfile
    import test_command_display_tui as test
    os.environ.pop('NO_COLOR', None)
    os.environ['FORCE_COLOR'] = '3'
    original_temp = tempfile.TemporaryDirectory
    tempfile.TemporaryDirectory = lambda *a, **kw: original_temp(prefix='', dir=args.short_root) if not a and 'dir' not in kw else original_temp(*a, **kw)
    original = test.snapshot
    colors = []
    def snapshot(terminal, evidence, name):
        original(terminal, evidence, name)
        if args.theme == 'morandi' and name == 'complete-before-contract' and evidence.name != 'explored':
            failed = evidence.name.endswith('failure')
            row = marker(terminal, 'Failed' if failed else 'Ran', 'd09a8c' if failed else 'a8c9ad')
            row['case'] = evidence.name
            colors.append(row)
            (evidence / 'completion-marker.json').write_text(json.dumps(row, indent=2) + '\n')
    test.snapshot = snapshot
    test.run(args.binary, args.launcher, args.evidence, test.CASES, args.theme)
    if args.theme == 'morandi':
        assert colors and any(row['title'] == 'Failed' for row in colors) and any(row['title'] == 'Ran' for row in colors)
        (args.evidence / 'completion-colors.json').write_text(json.dumps(dict(passed=True,
            samples=colors, success_matches_context_truecolor='a8c9ad', failure_red='d09a8c'), indent=2) + '\n')


if __name__ == '__main__':
    main()
