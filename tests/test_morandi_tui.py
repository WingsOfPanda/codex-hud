"""Morandi picker acceptance using existing private native-smoke fixtures.

Run with the native-smoke interpreter. --expect absent records the prechange
baseline; --expect present checks preview, cancellation, persistence and restart.
This test captures colors without inventing a palette acceptance threshold.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import tomllib

from fixtures.native_server import AppServer
from native_smoke import Terminal, composer_ready, wait
from test_ultracode_tui import calls, helper_script


ROOT = '00000000-0000-0000-0064-000000000001'
TITLE = 'Select Syntax Theme'


def first_column(row):
    return re.split(r' {2,}', row.lstrip(), maxsplit=1)[0]


def morandi_filter(terminal):
    rows = [first_column(row) for row in terminal.screen.display]
    complete = 'morandi' in rows
    selected = any(row.removeprefix('› ').replace(' (custom)', '').replace(' (current)', '')
                   == 'morandi' for row in rows if row.startswith('› '))
    return complete, selected, 'no matches' in rows


class ThemeServer(AppServer):
    def __init__(self, home, workspace):
        self.home = home
        socket = home / 'app-server-control/app-server-control.sock'
        socket.parent.mkdir(mode=0o700)
        super().__init__(ROOT, workspace, 0, socket_path=socket)
        socket.chmod(0o600)

    def reply(self, method, params):
        result = super().reply(method, params)
        if method == 'config/read':
            result['result']['config']['tui'] = tomllib.loads(
                (self.home / 'config.toml').read_text())['tui']
        return result


def colors(terminal, text):
    matches = [(y, row.index(text)) for y, row in enumerate(terminal.screen.display)
               if text in row]
    assert len(matches) == 1, (text, matches, terminal.text)
    y, x = matches[0]
    return [dict(character=cell.data, foreground=cell.fg, background=cell.bg,
                 bold=cell.bold, italics=cell.italics, reverse=cell.reverse)
            for cell in (terminal.screen.buffer[y][i] for i in range(x, x + len(text)))]


def capture(terminal, evidence, name):
    (evidence / f'{name}.txt').write_text(terminal.text + '\n')
    (evidence / f'{name}.ansi').write_bytes(terminal.output)
    cells = [[dict(character=cell.data, foreground=cell.fg, background=cell.bg,
                   bold=cell.bold, italics=cell.italics, reverse=cell.reverse)
              for x in range(terminal.screen.columns)
              for cell in [terminal.screen.buffer[y][x]]]
             for y in range(terminal.screen.lines)]
    (evidence / f'{name}-cells.json').write_text(json.dumps(cells, indent=2) + '\n')


def run(args):
    binary, evidence = args.binary.resolve(strict=True), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    report = dict(passed=False, expect=args.expect, binary=str(binary),
                  binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                  fixture='private PTY, existing app-server fixture and helper stub',
                  stock_compatibility_test=False, runtime_semantics_test=False,
                  palette_acceptance_test=False, stages=[])
    terminal = server = None
    with tempfile.TemporaryDirectory(prefix='plus-morandi-') as temporary:
        root = Path(temporary)
        home, workspace, package = [root / name for name in ('h', 'w', 'p')]
        for directory in (home, workspace, package):
            directory.mkdir(mode=0o700)
        native = package / 'codex'
        shutil.copy2(binary, native)
        host = binary.with_name('codex-code-mode-host')
        if host.exists():
            (package / host.name).symlink_to(host)
        helper = package / 'codex-plus-runtime'
        helper.write_text(helper_script((Path(__file__).resolve().parents[1] /
                                         'docs/ultracode-bootstrap.txt').read_text()))
        helper.chmod(0o700)
        config = home / 'config.toml'
        config.write_text('model="gpt-6-sol"\nmodel_provider="openai"\n'
            'check_for_update_on_startup=false\nsuppress_unstable_features_warning=true\n'
            'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
            '[tui]\ntheme="ada"\nstatus_line=["model-with-reasoning","current-dir"]\n'
            'status_line_use_colors=true\nscreen_reader_detection_done=true\n')
        (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
        os.environ.pop('NO_COLOR', None)
        os.environ['FORCE_COLOR'] = '3'
        server = ThemeServer(home, workspace)
        original_config = config.read_bytes()

        def start():
            current = Terminal(native, home, workspace)
            wait([current], lambda: composer_ready(current.text) and 'gpt-6-sol' in current.text
                 and not any('\u2800' <= character <= '\u28ff' for character in current.text)
                 and any(
                c['args'][:2] == ['ultracode', 'provenance'] for c in calls(home)),
                'private native startup did not settle', 25)
            return current

        def picker():
            os.write(terminal.master, b'/theme')
            wait([terminal], lambda: '/theme' in terminal.text, 'theme command absent')
            os.write(terminal.master, b'\r')
            wait([terminal], lambda: TITLE in terminal.text and
                 'fn summarize' in terminal.text, 'theme picker preview absent')

        def cancel():
            os.write(terminal.master, b'\x1b')
            wait([terminal], lambda: TITLE not in terminal.text and
                 composer_ready(terminal.text) and 'gpt-6-sol' in terminal.text
                 and 'enter select · esc back' not in terminal.text,
                 'picker did not cancel and restore the status footer')

        try:
            terminal = start()
            capture(terminal, evidence, 'ada-idle')
            idle_style = colors(terminal, 'gpt-6-sol')
            header_style = colors(terminal, 'OpenAI Codex')
            picker()
            initial_preview = colors(terminal, 'fn summarize')
            capture(terminal, evidence, 'ada-picker')
            os.write(terminal.master, b'morandi')
            if args.expect == 'absent':
                wait([terminal], lambda: morandi_filter(terminal) == (True, False, True),
                     'baseline unexpectedly has a matching Morandi theme')
                capture(terminal, evidence, 'morandi-absent')
                report['stages'].append('prechange-morandi-absent')
                cancel()
                assert config.read_bytes() == original_config
            else:
                wait([terminal], lambda: morandi_filter(terminal)[0] and
                     (morandi_filter(terminal)[1] or morandi_filter(terminal)[2]),
                     'completed Morandi filter did not render')
                assert morandi_filter(terminal) == (True, True, False), \
                    'Morandi absent from completed theme filter'
                report['stages'].append('exact-completed-filter-selects-morandi')
                wait([terminal], lambda:
                     colors(terminal, 'fn summarize') != initial_preview,
                     'Morandi did not produce a live syntax preview')
                capture(terminal, evidence, 'morandi-live-preview')
                assert config.read_bytes() == original_config, 'preview persisted a choice'
                report['stages'].append('live-preview-without-config-write')
                cancel()
                assert config.read_bytes() == original_config, 'Escape changed config'
                assert colors(terminal, 'gpt-6-sol') == idle_style, 'Escape left themed footer'
                assert colors(terminal, 'OpenAI Codex') == header_style, 'Escape left themed header'
                picker()
                assert colors(terminal, 'fn summarize') == initial_preview, 'Escape left syntax theme'
                report['stages'].append('escape-restores-syntax-and-footer')
                os.write(terminal.master, b'morandi')
                wait([terminal], lambda: morandi_filter(terminal) == (True, True, False) and
                     colors(terminal, 'fn summarize') != initial_preview,
                     'second Morandi preview absent')
                os.write(terminal.master, b'\r')
                wait([terminal], lambda: TITLE not in terminal.text and
                     tomllib.loads(config.read_text())['tui']['theme'].lower() == 'morandi',
                     'confirmed Morandi was not saved')
                capture(terminal, evidence, 'morandi-confirmed')
                saved = tomllib.loads(config.read_text())
                original = tomllib.loads(original_config.decode())
                original['tui']['theme'] = saved['tui']['theme']
                assert saved == original, 'theme confirmation changed unrelated config'
                report['stages'].append('confirm-persists-only-theme')
                replies = terminal.resize(24, 65)
                wait([terminal], lambda: terminal.resized_frame(replies) and
                     composer_ready(terminal.text), 'themed narrow frame did not finish')
                os.write(terminal.master, b'morandi-editable-draft')
                wait([terminal], lambda: 'morandi-editable-draft' in terminal.text,
                     'themed narrow composer is not editable')
                capture(terminal, evidence, 'morandi-narrow-editable')
                replies = terminal.resize(32, 120)
                wait([terminal], lambda: terminal.resized_frame(replies) and
                     'morandi-editable-draft' in terminal.text, 'resize lost themed composer draft')
                os.write(terminal.master, b'\x15')
                wait([terminal], lambda: 'morandi-editable-draft' not in terminal.text,
                     'composer draft did not clear')
                report['stages'].append('themed-resize-preserves-editable-draft')
                terminal.close()
                terminal = start()
                picker()
                assert colors(terminal, 'fn summarize') != initial_preview, 'restart lost Morandi'
                capture(terminal, evidence, 'morandi-after-restart')
                cancel()
                report['stages'].append('private-restart-restores-choice')
            assert not server.errors, server.errors
            forbidden = {'turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start'}
            assert not [r for r in server.requests if r.get('method') in forbidden]
            report.update(passed=True, display_model_requests=0)
        except BaseException as error:
            report['failure'] = str(error)
            raise
        finally:
            if terminal is not None:
                capture(terminal, evidence, 'last-screen')
                terminal.close()
            server.close()
            shutil.copy2(config, evidence / 'private-config.toml')
            (evidence / 'rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--expect', required=True, choices=['absent', 'present'])
    run(parser.parse_args())
