"""Prepared native PTY regression; imports the existing private smoke fixtures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--binary', required=True, type=Path)
parser.add_argument('--source', required=True, type=Path)
parser.add_argument('--evidence', required=True, type=Path)
parser.add_argument('--color-level', choices=['0', '1', '2', '3'], default='3')
parser.add_argument('--colors', choices=['on', 'off', 'observe'])
parser.add_argument('--no-color', action='store_true', help='Exercise NO_COLOR without FORCE_COLOR')
parser.add_argument('--case', choices=['all', 'mixed', 'stats-only', 'identity-only', 'branch', 'empty'], default='all')
args = parser.parse_args()
if args.colors is not None:
    os.environ.pop('NO_COLOR', None)
    os.environ['FORCE_COLOR'] = args.color_level
    if args.color_level == '0':
        os.environ['NO_COLOR'] = '1'
if args.no_color:
    os.environ.pop('FORCE_COLOR', None)
    os.environ['NO_COLOR'] = '1'
source = args.source.resolve(strict=True)
sys.path.insert(0, str(source / 'tests'))
from fixtures.native_server import AppServer, frame
from native_smoke import Terminal, composer_ready, wait, worker_rows, assert_compact_hud
from test_ultracode_tui import helper_script, calls, project_effective


ROOT = '00000000-0000-0000-0011-000000000001'
STATS = ['context-remaining', 'weekly-limit', 'weekly-reset']


class StatusServer(AppServer):
    def __init__(self, home, workspace, selection, workers):
        self.selection = selection
        self.reset = int(time.time()) + 5 * 86400 + 20 * 3600 + 120
        socket = home / 'app-server-control/app-server-control.sock'
        socket.parent.mkdir(mode=0o700)
        super().__init__(ROOT, workspace, workers, socket_path=socket)
        socket.chmod(0o600)

    def reply(self, method, params):
        if method == 'command/exec':
            assert 'git-branch' in self.selection, params
            assert params['cwd'] == self.cwd, params
            assert params['command'] == ['git', '-c', 'safe.bareRepository=explicit',
                                         'branch', '--show-current'], params
            output = subprocess.run(params['command'], cwd=self.cwd, capture_output=True,
                                    text=True, timeout=5,
                                    env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1',
                                         'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_OPTIONAL_LOCKS': '0'})
            return {'result': {'exitCode': output.returncode, 'stdout': output.stdout,
                               'stderr': output.stderr}}
        if method == 'account/read':
            return {'result': {'account': {'type': 'chatgpt', 'email': None, 'planType': 'plus'},
                               'requiresOpenaiAuth': True}}
        if method == 'account/rateLimits/read':
            snapshot = {'limitId': 'codex',
                'primary': {'usedPercent': 10, 'windowDurationMins': 300, 'resetsAt': self.reset},
                'secondary': {'usedPercent': 55, 'windowDurationMins': 10080, 'resetsAt': self.reset}}
            return {'result': {'rateLimits': snapshot, 'rateLimitsByLimitId': {'codex': snapshot}}}
        result = super().reply(method, params)
        if method == 'config/read':
            result['result']['config'].update(model='gpt-6-astra', model_reasoning_effort='high',
                tui={'status_line': self.selection, 'status_line_use_colors': args.colors != 'off'})
        if method in ('thread/start', 'thread/resume'):
            result['result'].update(model='gpt-6-astra', reasoningEffort='high')
        if method == 'thread/list' and params.get('ancestorThreadId') == ROOT:
            result['result']['data'] = [] if params.get('archived') else self.workers
        return result

    def context75(self):
        # (100000 - 34000) / (100000 - upstream baseline 12000) = 75%.
        usage = dict(totalTokens=34000, inputTokens=34000, cachedInputTokens=0,
                     outputTokens=0, reasoningOutputTokens=0)
        payload = {'method': 'thread/tokenUsage/updated', 'params': {'threadId': ROOT,
            'turnId': 'fixture-turn', 'tokenUsage': {'total': usage, 'last': usage,
                                                  'modelContextWindow': 100000}}}
        with self.lock:
            for connection in self.connections:
                connection.sendall(frame(json.dumps(payload).encode()))


def footer_rows(terminal):
    display = terminal.screen.display
    composer = max(i for i, line in enumerate(display) if line.lstrip().startswith('›'))
    workers = worker_rows(terminal.text)
    return [(i, line.strip()) for i, line in enumerate(display) if i > composer
            and line.strip() and line.strip() not in workers]



def item_colors(terminal, rows, branch=False):
    labels = ['gpt-6-astra high ultracode on', 'workspace', 'Main [default]',
              'Context 75% left', 'weekly 45% left', 'resets in 5d 20h']
    if branch:
        labels.append('main')
    found = {}
    for label in labels:
        matches = [(row, terminal.screen.display[row].index(label))
                   for row, _ in rows if label in terminal.screen.display[row]]
        assert len(matches) == 1, (label, matches)
        row, column = matches[0]
        cells = [terminal.screen.buffer[row][x] for x in range(column, column + len(label))]
        foregrounds = sorted({cell.fg for cell in cells})
        assert len(foregrounds) == 1, (label, foregrounds)
        found[label] = dict(foreground=foregrounds[0], background=sorted({cell.bg for cell in cells}),
                            bold=sorted({cell.bold for cell in cells}), italics=sorted({cell.italics for cell in cells}))
    separators = []
    for row, _ in rows:
        for column, cell in terminal.screen.buffer[row].items():
            if cell.data == '·':
                separators.append(cell.fg)
    return dict(items=found, separators=sorted(set(separators)),
                pairwise_distinct=len({value['foreground'] for value in found.values()}) == len(found))


def run_case(binary, directory, evidence, name):
    evidence.mkdir(parents=True, exist_ok=True)
    home, work = directory / 'home', directory / 'workspace'
    home.mkdir(mode=0o700)
    work.mkdir()
    if name == 'branch':
        subprocess.run(['git', '-c', 'init.templateDir=', 'init', '--initial-branch=main', str(work)],
                       check=True, capture_output=True, env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1',
                                                           'GIT_CONFIG_GLOBAL': os.devnull})
    selection = {'mixed': ['model-with-reasoning', 'context-remaining', 'current-dir',
                           'weekly-limit', 'weekly-reset'], 'stats-only': STATS,
                 'identity-only': ['model-with-reasoning', 'current-dir'],
                 'branch': ['model-with-reasoning', 'current-dir', 'git-branch', *STATS], 'empty': []}[name]
    # A readable off setting still has an identity badge above the stats row.
    workers = 2 if name in ('mixed', 'branch') else 0
    config = ('model="gpt-6-astra"\nmodel_reasoning_effort="high"\n'
        'model_provider="openai"\ncheck_for_update_on_startup=false\n'
        'suppress_unstable_features_warning=true\nanalytics.enabled=false\n'
        'features.daemon_auto_start=false\nfeatures.plugins=false\n'
        f'[tui]\nstatus_line={json.dumps(selection)}\n'
        + (f'status_line_use_colors={str(args.colors != "off").lower()}\n' if args.colors else ''))
    (home / 'config.toml').write_text(config)
    (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
    server = StatusServer(home, work, selection, workers)
    terminal = None
    report = dict(case=name, selection=selection, colors_mode=args.colors, color_level=args.color_level,
                  no_color=args.no_color, passed=False, stages=[])
    try:
        terminal = Terminal(binary, home, work)
        wait([terminal], lambda: composer_ready(terminal.text)
             and any(c['args'][:2] == ['ultracode', 'provenance'] for c in calls(home)),
             'native startup/provenance not ready', 25)
        replies = terminal.resize(32, 200)
        wait([terminal], lambda: terminal.resized_frame(replies), 'wide terminal not repainted')
        if name in ('mixed', 'branch'):
            os.write(terminal.master, b'/subagents')
            wait([terminal], lambda: '› /subagents' in terminal.text, 'agent picker command absent')
            os.write(terminal.master, b'\r')
            wait([terminal], lambda: any(r.get('params', {}).get('ancestorThreadId') == ROOT
                 and r.get('params', {}).get('archived') for r in server.requests),
                 'agent picker metadata not requested')
            os.write(terminal.master, b'\x1b')
            wait([terminal], lambda: composer_ready(terminal.text)
                 and 'Main [default]' in terminal.text, 'active root label not populated')
            projection = home / 'codex-plus/ultracode' / f'{ROOT}.json'
            wait([terminal], projection.exists,
                 'Ultracode projection not initialized')
            snapshot = json.loads(projection.read_text())
            snapshot['requested']['enabled'] = True
            projection.write_text(json.dumps(snapshot))
            project_effective(home, ROOT, True)
        if name in ('mixed', 'branch', 'stats-only'):
            server.context75()
            wait([terminal], lambda: all(text in terminal.text for text in
                 ('Context 75% left', 'weekly 45% left', 'resets in 5d 20h')),
                 'deterministic quota/context values absent', 20)
        wait([terminal], lambda: len(worker_rows(terminal.text)) == workers
             and any(r.get('method') == 'thread/list' and
                     r.get('params', {}).get('parentThreadId') == ROOT for r in server.requests)
             and any(r.get('method') == 'thread/backgroundTerminals/list' for r in server.requests)
             and 'counts unavailable' not in terminal.text,
             'native HUD metadata not settled')
        if name in ('mixed', 'branch'):
            wait([terminal], lambda: any('ultracode' in line.lower() for _, line in footer_rows(terminal)),
                 'effective Ultracode badge absent')
        if name == 'branch':
            wait([terminal], lambda: any(' · main' in line for _, line in footer_rows(terminal)),
                 'private repository branch absent')
        os.write(terminal.master, b'footer-editable-draft')
        wait([terminal], lambda: 'footer-editable-draft' in terminal.text, 'composer not editable')
        wait([terminal], lambda: 'counts unavailable' not in terminal.text,
             'HUD startup warning did not settle')
        rows = footer_rows(terminal)
        (evidence / 'wide.txt').write_text(terminal.text + '\n')
        report['wide_footer_rows'] = rows
        if name in ('mixed', 'branch'):
            assert len(rows) == 2, f'expected two physical footer rows, got {rows}'
            first, second = rows
            assert second[0] == first[0] + 1, f'footer rows not adjacent: {rows}'
            assert all(value in first[1].lower() for value in ('gpt-6-astra high ultracode on', 'workspace', 'main [default]')), rows
            if name == 'branch':
                assert ' · main' in first[1], rows
            assert not any(value in first[1] for value in ('Context ', 'weekly ', 'resets ')), rows
            assert second[1] == 'Context 75% left · weekly 45% left · resets in 5d 20h', rows
        elif name == 'stats-only':
            assert len(rows) == 2 and rows[0][1] == 'ultracode off', rows
            assert rows[1][0] == rows[0][0] + 1, rows
            assert rows[1][1] == 'Context 75% left · weekly 45% left · resets in 5d 20h', rows
        elif name == 'identity-only':
            assert len(rows) == 1 and 'workspace' in rows[0][1], rows
        else:
            assert rows == [], f'disabled status left a footer row: {rows}'
        report['stages'].append('wide-physical-rows')
        if name in ('mixed', 'branch') and args.colors:
            report['colors'] = item_colors(terminal, rows, branch=name == 'branch')
            (evidence / 'colors.json').write_text(json.dumps(report['colors'], indent=2) + '\n')
        assert_compact_hud(terminal.text, rows=workers)
        if workers:
            worker_indices = [i for i, line in enumerate(terminal.screen.display)
                              if line.strip() in worker_rows(terminal.text)]
            assert min(worker_indices) > rows[-1][0], 'workers overlap or precede status rows'
        replies = terminal.resize(12, 65)
        wait([terminal], lambda: terminal.resized_frame(replies)
             and 'footer-editable-draft' in terminal.text, 'resize lost editable draft')
        os.write(terminal.master, b'-narrow')
        wait([terminal], lambda: 'footer-editable-draft-narrow' in terminal.text, 'resized composer not editable')
        narrow = footer_rows(terminal)
        assert len(narrow) == len(rows), f'narrow resize changed row count: {narrow}'
        if name in ('mixed', 'branch'):
            assert 'Context 75% left' in narrow[1][1] and 'Context ' not in narrow[0][1], narrow
        assert_compact_hud(terminal.text, rows=workers)
        (evidence / 'narrow.txt').write_text(terminal.text + '\n')
        report['stages'].append('narrow-editable-input')
        if name in ('mixed', 'branch'):
            replies = terminal.resize(32, 200)
            wait([terminal], lambda: terminal.resized_frame(replies), 'wide terminal not repainted for shortcuts')
            os.write(terminal.master, b'\x15?')
            wait([terminal], lambda: 'Keyboard shortcuts' in terminal.text, 'shortcuts overlay absent')
            assert len([line for _, line in footer_rows(terminal) if 'Context 75% left' in line]) == 1
            (evidence / 'shortcuts.txt').write_text(terminal.text + '\n')
            os.write(terminal.master, b'\x1b')
            wait([terminal], lambda: 'Keyboard shortcuts' not in terminal.text, 'shortcuts did not close')
            report['stages'].append('shortcuts-persistent-status')
        assert not server.errors, server.errors
        forbidden = ('turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start')
        assert not [request for request in server.requests if request.get('method') in forbidden]
        saved = tomllib.loads((home / 'config.toml').read_text())
        assert saved['tui']['status_line'] == selection, 'display rewrote status-line selection'
        report.update(display_model_requests=0, config_preserved=True)
        if name in ('mixed', 'branch') and args.colors == 'on' and args.color_level != '0' and not args.no_color:
            assert report['colors']['pairwise_distinct'], report['colors']
            assert len(report['colors']['separators']) == 1, report['colors']
            if name == 'branch':
                foreground = report['colors']['items']['main']['foreground']
                if args.color_level == '1':
                    assert foreground == 'ff0000', report['colors']
                else:
                    # Two near-blue RGB values are distinct numbers but still look alike.
                    assert int(foreground[:2], 16) > int(foreground[4:], 16) + 40, report['colors']
        if name in ('mixed', 'branch') and (args.colors == 'off' or args.color_level == '0' or args.no_color):
            foregrounds = {value['foreground'] for value in report['colors']['items'].values()}
            assert len(foregrounds) == 1, report['colors']
            assert foregrounds == set(report['colors']['separators']), report['colors']
        report['passed'] = True
    except BaseException as error:
        report['failure'] = str(error)
        raise
    finally:
        if terminal:
            (evidence / 'last-screen.txt').write_text(terminal.text + '\n')
            (evidence / 'terminal.ansi').write_bytes(terminal.output)
            terminal.close()
        server.close()
        report['fixture_errors'] = server.errors
        (evidence / 'rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def run():
    binary, evidence = args.binary.resolve(strict=True), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    report = dict(binary=str(binary), binary_sha256=hashlib.file_digest(binary.open('rb'), 'sha256').hexdigest(),
                  fixture='private native PTY, app-server fixture and helper stub; no stock/runtime claim', cases=[])
    with tempfile.TemporaryDirectory(prefix='plus-status-') as temporary:
        root = Path(temporary)
        package = root / 'package'
        package.mkdir()
        native = package / 'codex'
        shutil.copy2(binary, native)
        host = binary.with_name('codex-code-mode-host')
        if host.exists():
            (package / host.name).symlink_to(host)
        helper = package / 'codex-plus-runtime'
        helper.write_text(helper_script((source / 'docs/ultracode-bootstrap.txt').read_text()))
        helper.chmod(0o700)
        names = ['mixed', 'stats-only', 'identity-only', 'branch', 'empty'] if args.case == 'all' else [args.case]
        for name in names:
            directory = root / name
            directory.mkdir()
            report['cases'].append(run_case(native, directory, evidence / name, name))
    report['passed'] = True
    (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


run()
