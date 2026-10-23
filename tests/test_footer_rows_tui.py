"""Private PTY contract: distinct activity rows survive warnings, Advisor and typing."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--source', type=Path, required=True)
p.add_argument('--binary', type=Path, required=True)
p.add_argument('--evidence', type=Path, required=True)
a = p.parse_args()
a.evidence.mkdir(parents=True, exist_ok=False)
sys.path.insert(0, str(a.source / 'tests'))
from fixtures.native_server import AppServer, frame
from native_smoke import Terminal, wait, composer_ready, worker_rows, write_health, write_model_catalog
from test_ultracode_tui import helper_script

ROOT = '00000000-0000-0000-0043-000000000001'
SELECTION = ['model-with-reasoning', 'current-dir', 'context-remaining', 'weekly-limit']


class Server(AppServer):
    def reply(self, method, params):
        r = super().reply(method, params)
        if method == 'config/read':
            r['result']['config']['tui'] = {'status_line': SELECTION}
        return r

    def notify(self, method, params):
        with self.lock:
            for connection in self.connections:
                connection.sendall(frame(json.dumps({'method': method, 'params': params}).encode()))


report = {'passed': False, 'stages': [], 'violations': []}
with tempfile.TemporaryDirectory(prefix='plus-rows-') as temporary:
    root = Path(temporary)
    home, work, package = [root / n for n in ('home', 'work', 'package')]
    for d in (home, work, package):
        d.mkdir()
    native = package / 'codex'
    shutil.copy2(a.binary, native)
    host = a.binary.with_name('codex-code-mode-host')
    if host.exists():
        (package / host.name).symlink_to(host)
    helper = package / 'codex-plus-runtime'
    helper.write_text(helper_script((a.source / 'docs/ultracode-bootstrap.txt').read_text()))
    helper.chmod(0o700)
    (home / 'config.toml').write_text('model="gpt-6.1-sol"\nmodel_provider="openai"\n'
        'check_for_update_on_startup=false\nsuppress_unstable_features_warning=true\n'
        'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
        + write_model_catalog(home, astra=True)
        + '[tui]\nstatus_line=' + json.dumps(SELECTION) + '\n')
    socket = home / 'app-server-control/app-server-control.sock'
    socket.parent.mkdir()
    server = Server(ROOT, work, 6, socket_path=socket)
    socket.chmod(0o600)
    server.shells = [server.shell(701, 'shell-contract')]
    write_health(home, ROOT, 'idle')
    now = time.time()
    (home / 'codex-plus' / f'{ROOT}.advisor.json').write_text(json.dumps(dict(
        version=1, parent=ROOT, model='gpt-6-astra', effort='xhigh', state='done',
        started_at=now - 5, updated_at=now, finished_at=now, calls=1,
        last_duration_secs=5, error=None)))
    directory = home / 'codex-plus/execution-status' / ROOT
    directory.mkdir(parents=True)
    for i in range(2):
        uid = f'00000000-0000-4000-8000-00000000000{i}'
        now = time.time()
        (directory / f'{uid}.json').write_text(json.dumps(dict(schema=1, id=uid, root=ROOT,
            topic='detached-contract-' + str(i), state='unknown' if i == 0 else 'running',
            started_at=now - 5, updated_at=now, error='fixture unresolved' if i == 0 else None)))
    terminal = None

    def settle():
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            terminal.read()

    def capture(name, *, draft=False, busy=False):
        lines = terminal.screen.display
        prompt = max(i for i, line in enumerate(lines) if line.lstrip().startswith('›'))
        below = [line.strip() for line in lines[prompt + 1:] if line.strip()]
        text = '\n'.join(below)
        workers = worker_rows(text)
        stages = dict(name=name, below=below, count=len(below), workers=len(workers))
        report['stages'].append(stages)
        (a.evidence / (name + '.txt')).write_text(terminal.text)
        checks = {
            'subagent row visible': bool(workers),
            'aggregate background warning hidden': 'Background status unavailable' not in text,
            'affected job remains visible': 'Background · detached-contract-0 · needs review · ?' in below,
            'native shell visible': any(x.startswith('Background · shell-contract') for x in below),
            'detached job visible': any(x.startswith('Background · detached-contract-') for x in below),
            'advisor own row': any(x.startswith('Advisor ·') for x in below),
            'overflow own row': any(re.fullmatch(r'\+\d+ more', x) for x in below),
            'no overflow merged into advisor': not any('more' in x and 'Advisor' in x for x in below),
            'automatic queue hint hidden': 'tab to queue message' not in text,
            'footer bounded': len(below) <= (9 if name == 'explicit-help' else 8),
            'draft editable': not draft or 'rows-editable' in terminal.text,
            'busy notification accepted': not busy or 'esc to interrupt' in terminal.text,
        }
        shown = len(workers) + sum(x.startswith('Background · ') for x in below)
        checks['overflow exact'] = any(x == f'+{9 - shown} more' for x in below)
        for label, passed in checks.items():
            if not passed:
                report['violations'].append(name + ': ' + label)

    try:
        terminal = Terminal(native, home, work)
        wait([terminal], lambda: composer_ready(terminal.text) and 'Advisor' in terminal.text
             and 'more' in terminal.text,
             'mixed activity fixture did not settle', 30)
        settle()
        capture('idle')
        server.notify('turn/started', {'threadId': ROOT, 'turn': {'id': 'fixture-busy',
            'items': [], 'status': 'inProgress', 'error': None}})
        os.write(terminal.master, b'rows-editable')
        wait([terminal], lambda: 'rows-editable' in terminal.text and 'esc to interrupt' in terminal.text,
             'busy draft did not appear')
        settle()
        capture('busy-draft', draft=True, busy=True)
        terminal.resize(20, 75)
        settle()
        capture('narrow-busy-draft', draft=True, busy=True)
        server.notify('turn/completed', {'threadId': ROOT, 'turn': {'id': 'fixture-busy',
            'items': [], 'status': 'completed', 'error': None}})
        terminal.resize(32, 120)
        settle()
        os.write(terminal.master, b'\x15?')
        wait([terminal], lambda: 'Keyboard shortcuts' in terminal.text, 'explicit help unavailable')
        settle()
        capture('explicit-help')
        forbidden = {'turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start'}
        assert not [r for r in server.requests if r['method'] in forbidden], 'display submitted input'
        assert not server.errors, server.errors
        report['display_model_requests'] = 0
    except BaseException as error:
        report['failure'] = str(error)
        raise
    finally:
        if terminal:
            (a.evidence / 'last-screen.txt').write_text(terminal.text)
            (a.evidence / 'terminal.ansi').write_bytes(terminal.output)
            terminal.close()
        server.close()
        report['passed'] = not report['violations'] and 'failure' not in report
        (a.evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
assert report['passed'], report['violations']
