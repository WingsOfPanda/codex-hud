"""Private candidate PTY check for AP's last-six-nonempty-lines idle detector."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import tomllib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', required=True, type=Path)
parser.add_argument('--binary', required=True, type=Path)
parser.add_argument('--evidence', required=True, type=Path)
args = parser.parse_args()
source, binary, evidence = args.source.resolve(), args.binary.resolve(), args.evidence.resolve()
evidence.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(source / 'tests'))
from fixtures.native_server import AppServer, frame
from native_smoke import Terminal, composer_ready, wait, worker_rows, write_health
from test_ultracode_tui import helper_script, calls

ROOT = '00000000-0000-0000-0042-000000000001'
SELECTION = ['model-with-reasoning', 'context-remaining', 'weekly-limit',
             'current-dir', 'pull-request-number', 'weekly-reset']


class FleetServer(AppServer):
    def reply(self, method, params):
        if method == 'account/read':
            return {'result': {'account': {'type': 'chatgpt', 'email': None, 'planType': 'plus'},
                               'requiresOpenaiAuth': True}}
        if method == 'account/rateLimits/read':
            window = {'usedPercent': 55, 'windowDurationMins': 10080,
                      'resetsAt': int(time.time()) + 5 * 86400 + 20 * 3600 + 120}
            value = {'limitId': 'codex', 'primary': window, 'secondary': window}
            return {'result': {'rateLimits': value, 'rateLimitsByLimitId': {'codex': value}}}
        result = super().reply(method, params)
        if method == 'config/read':
            result['result']['config'].update(model='gpt-6-astra', model_reasoning_effort='high',
                tui={'status_line': SELECTION, 'status_line_use_colors': True})
        if method in ('thread/start', 'thread/resume'):
            result['result'].update(model='gpt-6-astra', reasoningEffort='high')
        return result

    def context75(self):
        usage = dict(totalTokens=34000, inputTokens=34000, cachedInputTokens=0,
                     outputTokens=0, reasoningOutputTokens=0)
        payload = {'method': 'thread/tokenUsage/updated', 'params': {'threadId': ROOT,
            'turnId': 'fixture-turn', 'tokenUsage': {'total': usage, 'last': usage,
                                                  'modelContextWindow': 100000}}}
        with self.lock:
            for connection in self.connections:
                connection.sendall(frame(json.dumps(payload).encode()))


report = dict(passed=False, source=str(source), binary=str(binary),
    binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
    fixture='private PTY with native_smoke loopback fixture and helper stub; no stock/runtime claim',
    selection=SELECTION, status_line_use_colors=True, stages=[], violations=[])
terminal = server = None
with tempfile.TemporaryDirectory(prefix='plus-fleet-footer-') as temporary:
    directory = Path(temporary)
    home, work, package = (directory / name for name in ('home', 'workspace', 'package'))
    for path in (home, work, package):
        path.mkdir(mode=0o700)
    native = package / 'codex'
    shutil.copy2(binary, native)
    host = binary.with_name('codex-code-mode-host')
    if host.exists():
        (package / host.name).symlink_to(host)
    helper = package / 'codex-plus-runtime'
    helper.write_text(helper_script((source / 'docs/ultracode-bootstrap.txt').read_text()))
    helper.chmod(0o700)
    config = ('model="gpt-6-astra"\nmodel_reasoning_effort="high"\nmodel_provider="openai"\n'
        'check_for_update_on_startup=false\nsuppress_unstable_features_warning=true\n'
        'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
        '[tui]\nstatus_line=' + json.dumps(SELECTION) + '\nstatus_line_use_colors=true\n')
    (home / 'config.toml').write_text(config)
    (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
    socket = home / 'app-server-control/app-server-control.sock'
    socket.parent.mkdir(mode=0o700)
    server = FleetServer(ROOT, work, 6, socket_path=socket)
    socket.chmod(0o600)
    projection = home / 'codex-plus/goals' / f'{ROOT}.json'
    projection.parent.mkdir(parents=True)
    write_health(home, ROOT, 'idle')
    projection.write_text(json.dumps(dict(version=1, root=ROOT, attention=None,
        goal=dict(id='private-projection-goal', revision=1, state='active',
                  objective='Private AP idle detector fixture', running_jobs=0))))
    os.environ.pop('NO_COLOR', None)
    os.environ['FORCE_COLOR'] = '3'

    def capture(name, *, normal=False):
        display = terminal.screen.display
        prompt = max(i for i, line in enumerate(display) if line.lstrip().startswith('›'))
        below = [(i, line.strip()) for i, line in enumerate(display) if i > prompt and line.strip()]
        goal = [i for i, line in enumerate(display) if 'Goal ' in line]
        workers = [i for i, line in enumerate(display) if line.strip() in worker_rows(terminal.text)]
        overflow = [i for i, line in enumerate(display) if re.fullmatch(r'\+\d+ more', line.strip())]
        item = dict(name=name, dimensions=[terminal.screen.lines, terminal.screen.columns],
            prompt_row=prompt, goal_rows=goal, worker_rows=workers, overflow_rows=overflow,
            nonempty_below_prompt=len(below), below_prompt=below,
            prompt_in_last_six_nonempty=len(below) <= 5)
        report['stages'].append(item)
        (evidence / f'{name}-screen.txt').write_text(terminal.text + '\n')
        (evidence / f'{name}.ansi').write_bytes(terminal.output)
        try:
            assert len(below) <= 5, f'{name}: {len(below)} nonempty lines below prompt'
            assert goal and goal[0] < prompt, f'{name}: goal absent or below prompt'
            assert 1 <= len(workers) <= 2 and len(overflow) == 1, f'{name}: bounded worker strip missing'
            assert display[overflow[0]].strip() == f'+{6 - len(workers)} more', 'wrong hidden worker count'
            if normal:
                assert len(workers) == 2, f'{name}: normal worker budget unexpectedly reduced'
                assert len(below) == 5, (name, below)
                assert 'Context 75% left' in below[1][1], (name, below)
        except AssertionError as error:
            report['violations'].append(str(error))

    try:
        terminal = Terminal(native, home, work)
        wait([terminal], lambda: composer_ready(terminal.text) and '+4 more' in terminal.text
            and 'Goal active' in terminal.text and 'counts unavailable' not in terminal.text
            and any(c['args'][:2] == ['ultracode', 'provenance'] for c in calls(home)),
            'candidate private startup not settled', 30)
        server.context75()
        wait([terminal], lambda: 'Context 75% left' in terminal.text and 'weekly 45% left' in terminal.text,
             'deterministic status values absent')
        # Startup's transient sparkle is not an idle footer row.
        wait([terminal], lambda: not any('\u2800' <= c <= '\u28ff' for c in terminal.text),
             'startup sparkle did not settle')
        capture('wide-empty', normal=True)
        os.write(terminal.master, b'fleet-editable-draft')
        wait([terminal], lambda: 'fleet-editable-draft' in terminal.text, 'draft not editable')
        capture('wide-draft', normal=True)
        replies = terminal.resize(12, 65)
        wait([terminal], lambda: terminal.resized_frame(replies) and 'fleet-editable-draft' in terminal.text,
             'narrow resize did not preserve draft')
        os.write(terminal.master, b'-narrow')
        wait([terminal], lambda: 'fleet-editable-draft-narrow' in terminal.text, 'narrow not editable')
        capture('narrow-draft', normal=True)
        replies = terminal.resize(32, 120)
        wait([terminal], lambda: terminal.resized_frame(replies), 'wide resize not repainted')
        os.write(terminal.master, b'\x15?')
        wait([terminal], lambda: 'Keyboard shortcuts' in terminal.text, 'help not opened')
        capture('shortcuts')
        replies = terminal.resize(32, 65)
        wait([terminal], lambda: terminal.resized_frame(replies) and 'Keyboard shortcuts' in terminal.text,
             'narrow help did not repaint')
        capture('narrow-shortcuts')
        os.write(terminal.master, b'\x1b')
        wait([terminal], lambda: 'Keyboard shortcuts' not in terminal.text, 'help not closed')
        replies = terminal.resize(32, 120)
        wait([terminal], lambda: terminal.resized_frame(replies), 'wide help-close did not repaint')
        os.write(terminal.master, b'\x1b')
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            terminal.read()
        capture('escape-idle')
        os.write(terminal.master, b'\x12')
        wait([terminal], lambda: 'reverse-i-search:' in terminal.text, 'history footer not shown')
        capture('history-search')
        os.write(terminal.master, b'\x1b')
        wait([terminal], lambda: 'reverse-i-search:' not in terminal.text, 'history footer not closed')
        before_save = tomllib.loads((home / 'config.toml').read_text())
        (evidence / 'before-save-config.toml').write_text((home / 'config.toml').read_text())
        os.write(terminal.master, b'/statusline')
        wait([terminal], lambda: '› /statusline' in terminal.text, 'statusline command absent')
        os.write(terminal.master, b'\r')
        wait([terminal], lambda: 'Configure Status Line' in terminal.text, 'statusline picker absent')
        (evidence / 'statusline-picker-screen.txt').write_text(terminal.text + '\n')
        os.write(terminal.master, b'\r')
        wait([terminal], lambda: composer_ready(terminal.text) and 'Configure Status Line' not in terminal.text,
             'statusline save did not return to composer')
        forbidden = {'turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start'}
        assert not [r for r in server.requests if r['method'] in forbidden]
        assert not server.errors, server.errors
        saved = tomllib.loads((home / 'config.toml').read_text())
        (evidence / 'saved-config.toml').write_text((home / 'config.toml').read_text())
        assert saved['tui']['status_line'] == SELECTION, saved
        assert saved['tui']['status_line_use_colors'] is True, saved
        assert saved == before_save, 'statusline save changed configuration values or keys'
        report.update(config_preserved=True, save_introduced_new_keys=False, display_model_requests=0,
            startup_added_tui_keys=sorted(set(before_save['tui']) - set(tomllib.loads(config)['tui'])))
        os.write(terminal.master, b'\x03')
        deadline = time.monotonic() + 10
        while terminal.process.poll() is None and time.monotonic() < deadline:
            terminal.read()
        assert terminal.process.poll() == 0, 'idle Ctrl-C did not exit successfully'
        terminal.read()
        (evidence / 'quit-exit-screen.txt').write_text(terminal.text + '\n')
        report['quit_hint'] = 'Disabled in candidate; actual idle Ctrl-C exits zero instead of showing a footer'
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
        report['passed'] = not report['violations'] and 'failure' not in report
        (evidence / 'rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
assert report['passed'], report['violations']
