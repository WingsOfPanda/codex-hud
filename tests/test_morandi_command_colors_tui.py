"""Check actual command dots against the Context green using the existing private PTY fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

import test_command_display_tui as display


def run(binary, launcher, evidence, level):
    evidence.mkdir(parents=True, exist_ok=False)
    report = dict(passed=False, color_level=level, cases=[], colors={},
                  binary_sha256=hashlib.file_digest(binary.open('rb'), 'sha256').hexdigest())
    configure, snapshot = display.configure, display.snapshot

    def with_context(home, workspace, provider):
        configure(home, workspace, provider)
        config = home / 'config.toml'
        text = config.read_text()
        assert text.count('tui.status_line=["thread-name"]') == 1
        config.write_text('model_context_window=100000\n' + text.replace(
            'tui.status_line=["thread-name"]',
            'tui.status_line=["thread-name","context-remaining"]'))

    def with_colors(terminal, directory, name):
        snapshot(terminal, directory, name)
        if name != 'complete-before-contract':
            return
        rows = terminal.screen.display
        headers = [(y, row.index('•')) for y, row in enumerate(rows)
                   if re.match(r'^\s*• (Ran|Failed)\b', row)]
        contexts = [(y, row.index('Context ')) for y, row in enumerate(rows) if 'Context ' in row]
        assert len(headers) == len(contexts) == 1, terminal.text
        color = lambda position: terminal.screen.buffer[position[0]][position[1]].fg
        colors = dict(dot=color(headers[0]), context=color(contexts[0]))
        report['colors'][directory.name] = colors
        (directory / 'outcome-colors.json').write_text(json.dumps(colors, indent=2) + '\n')

    try:
        with tempfile.TemporaryDirectory(prefix='mc-') as temporary:
            with patch.object(display, 'configure', with_context), patch.object(display, 'snapshot', with_colors):
                for case in ('unmarked_success', 'unmarked_failure'):
                    root = Path(temporary) / case
                    root.mkdir()
                    report['cases'].append(display.run_case(binary, launcher, root, evidence / case, case, 'morandi'))
        success, failure = (report['colors'][case] for case in ('unmarked_success', 'unmarked_failure'))
        assert success['dot'] == success['context'] == failure['context'], report['colors']
        assert failure['dot'] != success['dot'], report['colors']
        red, green, blue = (int(success['dot'][i:i + 2], 16) for i in (0, 2, 4))
        assert green > red and green > blue, report['colors']
        red, green, blue = (int(failure['dot'][i:i + 2], 16) for i in (0, 2, 4))
        assert red > green and red > blue, report['colors']
        if level == '3':
            assert success['dot'] == 'a8c9ad' and failure['dot'] == 'd09a8c', report['colors']
        report['passed'] = True
    finally:
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--color-level', choices=['2', '3'], required=True)
    args = parser.parse_args()
    os.environ.pop('NO_COLOR', None)
    os.environ['FORCE_COLOR'] = args.color_level
    run(args.binary.resolve(strict=True), args.launcher.resolve(strict=True),
        args.evidence.resolve(), args.color_level)
