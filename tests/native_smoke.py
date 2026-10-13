"""Real native TUI acceptance with isolated homes and loopback protocol fixtures.

Setup: python3 -m venv /tmp/codex-hud-native-smoke-venv
       /tmp/codex-hud-native-smoke-venv/bin/pip install pyte==0.8.2
Run:   /tmp/codex-hud-native-smoke-venv/bin/python tests/native_smoke.py BINARY
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import sqlite3
import struct
import subprocess
import tempfile
import termios
import time

import pyte

from fixtures.native_server import AppServer


class Terminal:
    def __init__(self, binary, home, cwd, endpoint):
        self.master, slave = pty.openpty()
        self.screen = pyte.Screen(120, 32)
        self.cursor_replies, self.cursor_reply_offset = 0, 0
        self.screen.write_process_input = self.reply
        self.stream = pyte.ByteStream(self.screen)
        self.output, self.answered = bytearray(), set()
        self.resize(32, 120, notify=False)
        env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE', 'STY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY')}
        env.update(CODEX_HOME=str(home), TERM='xterm-256color', TERM_PROGRAM='kitty', OPENAI_API_KEY='isolated-fixture-only')
        self.process = subprocess.Popen([str(binary), '--remote', endpoint, '--yolo', '-C', str(cwd)], stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)

    def resize(self, rows, columns, notify=True):
        replies_before_resize = self.cursor_replies
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        self.screen.resize(rows, columns)
        if notify:
            self.process.send_signal(signal.SIGWINCH)
        return replies_before_resize

    def reply(self, data):
        if data.endswith('R'):
            self.cursor_replies += 1
            self.cursor_reply_offset = len(self.output)
        os.write(self.master, data.encode())

    def resized_frame(self, replies_before_resize):
        return self.cursor_replies > replies_before_resize and b'\x1b[?2026l' in self.output[self.cursor_reply_offset:]

    def read(self):
        if not select.select([self.master], [], [], .03)[0]:
            return
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            chunk = b''
        self.output.extend(chunk)
        self.stream.feed(chunk)
        for query, response in [(b'\x1b[?u', b'\x1b[?7u\x1b[?1;2c'), (b'\x1b]11;?', b'\x1b]10;rgb:ffff/ffff/ffff\x1b\\\x1b]11;rgb:0000/0000/0000\x1b\\')]:
            if query not in self.answered and query in self.output:
                os.write(self.master, response)
                self.answered.add(query)

    @property
    def text(self):
        return '\n'.join(self.screen.display)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        os.close(self.master)


def wait(terminals, predicate, reason, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for terminal in terminals:
            terminal.read()
            assert terminal.process.poll() is None, f'TUI exited ({terminal.process.returncode})\n{terminal.text}'
        if predicate():
            return
    raise AssertionError(f'{reason}\n' + '\n--- terminal ---\n'.join(terminal.text for terminal in terminals))


def tmux_smoke(binary, root, evidence):
    """Use a private tmux server; never touch the user's server or sessions."""
    if not shutil.which('tmux'):
        return ['tmux split check skipped: tmux is not installed']
    socket_path = root / 'tmux.sock'
    servers = []

    def tmux(*args, check=True):
        return subprocess.run(['tmux', '-S', str(socket_path), '-f', '/dev/null', *args], check=check, capture_output=True, text=True).stdout

    def screen(pane):
        return tmux('capture-pane', '-p', '-t', pane)

    def wait_screen(predicate, reason, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            screens = [screen(pane) for pane in panes]
            if predicate(screens):
                return screens
            time.sleep(.05)
        raise AssertionError(f'{reason}\n' + '\n--- tmux pane ---\n'.join(screens))

    try:
        commands = []
        for index, count in enumerate((2, 3)):
            home, workspace = root / f'tmux-home-{index}', root / f'tmux-workspace-{index}'
            home.mkdir()
            workspace.mkdir()
            (home / 'config.toml').write_text(f'model="gpt-6-sol"\nmodel_provider="openai"\nsuppress_unstable_features_warning=true\nanalytics.enabled=false\nfeatures.daemon_auto_start=false\n[projects."{workspace}"]\ntrust_level="trusted"\n')
            (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
            server = AppServer(f'00000000-0000-0000-000{index + 2}-000000000001', workspace, count)
            servers.append(server)
            commands.append(shlex.join(['env', '-u', 'OPENAI_BASE_URL', '-u', 'CODEX_API_KEY', f'CODEX_HOME={home}', 'OPENAI_API_KEY=isolated-fixture-only', 'TERM=screen-256color', 'TERM_PROGRAM=tmux', str(binary), '--remote', server.url, '--yolo', '-C', str(workspace)]))
        first = tmux('new-session', '-d', '-P', '-F', '#{pane_id}', '-x', '180', '-y', '36', '-s', 'native', commands[0]).strip()
        second = tmux('split-window', '-h', '-P', '-F', '#{pane_id}', '-t', first, commands[1]).strip()
        panes = [first, second]
        wait_screen(lambda screens: 'Workers 2' in screens[0] and 'Workers 3' in screens[1], 'tmux split HUD counts unavailable', 30)
        for index, pane in enumerate(panes):
            tmux('send-keys', '-l', '-t', pane, f'tmux-draft-{index}')
        wait_screen(lambda screens: all(f'tmux-draft-{index}' in text for index, text in enumerate(screens)), 'tmux composer unavailable')
        tmux('resize-window', '-t', 'native:0', '-x', '110', '-y', '18')
        wait_screen(lambda screens: all(f'tmux-draft-{index}' in text and 'Workers' in text for index, text in enumerate(screens)), 'tmux small resize lost HUD or composer')
        tmux('resize-window', '-t', 'native:0', '-x', '180', '-y', '36')
        wait_screen(lambda screens: 'Workers 2' in screens[0] and 'Workers 3' in screens[1], 'tmux normal resize lost counts')
        servers[0].disconnect()
        wait_screen(lambda screens: 'Workers stale' in screens[0] and 'Workers 3' in screens[1], 'tmux disconnect affected wrong pane')
        tmux('send-keys', '-l', '-t', first, 'offline')
        wait_screen(lambda screens: 'tmux-draft-0offline' in screens[0], 'tmux disconnected composer unavailable')
        servers[0].available.set()
        screens = wait_screen(lambda screens: 'Workers 2' in screens[0] and 'tmux-draft-0offline' in screens[0] and 'Workers 3' in screens[1] and 'tmux-draft-1' in screens[1], 'tmux reconnect failed', 30)
        for index, text in enumerate(screens):
            (evidence / f'tmux-pane-{index}-screen.txt').write_text(text)
        for server in servers:
            assert not server.errors, server.errors
            assert not any(request.get('method') in ('turn/start', 'turn/steer') for request in server.requests), server.requests
        return ['isolated tmux server: two split panes, draft editing, small/normal resize, stale/reconnect, zero turn/start or turn/steer']
    finally:
        tmux('kill-server', check=False)
        for index, server in enumerate(servers):
            server.close()
            (evidence / f'tmux-rpc-{index}.json').write_text(json.dumps(server.requests, indent=2) + '\n')


def run(binary, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    terminals, servers, stages = [], [], []
    (evidence / 'report.json').unlink(missing_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='codex-hud-smoke-')
    try:
        root = Path(temporary.name)
        for index, count in enumerate((2, 6)):
            home, workspace = root / f'home-{index}', root / f'workspace-{index}'
            home.mkdir()
            workspace.mkdir()
            (home / 'config.toml').write_text(f'model="gpt-6-sol"\nmodel_provider="openai"\nsuppress_unstable_features_warning=true\nanalytics.enabled=false\nfeatures.daemon_auto_start=false\n[projects."{workspace}"]\ntrust_level="trusted"\n')
            (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
            identifier = f'00000000-0000-0000-000{index}-000000000001'
            server = AppServer(identifier, workspace, count)
            # A remote worker must never display an unrelated local cache's tokens.
            with sqlite3.connect(home / 'state_5.sqlite') as database:
                database.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, tokens_used INTEGER NOT NULL)')
                database.executemany('INSERT INTO threads VALUES (?, ?)', [(worker['id'], 987654) for worker in server.workers])
            servers.append(server)
            terminals.append(Terminal(binary, home, workspace, server.url))

        wait(terminals, lambda: all(f'Workers {len(server.workers)}' in terminal.text for server, terminal in zip(servers, terminals)), 'distinct worker counts did not render', 30)
        for server, terminal in zip(servers, terminals):
            assert 'for shortcuts' in terminal.text, terminal.text
            assert terminal.text.index('for shortcuts') < terminal.text.index('Workers '), 'HUD must appear below the shortcuts footer\n' + terminal.text
            worker_lines = [line.strip() for line in terminal.screen.display if line.strip().startswith('worker ')]
            assert len(worker_lines) == min(3, len(server.workers)), terminal.text
            for index, line in enumerate(worker_lines):
                assert line.startswith(f'worker {index + 1} ·'), line
                assert '? tok' in line and ('Sol high' if index % 2 == 0 else 'Astra medium') in line, line
                assert re.search(r' · \d+:\d{2} · ', line), line
                assert '/root/' not in line and 'worker_' not in line, line
            if len(server.workers) > 3:
                assert '+3 more' in terminal.text and 'worker 4' not in terminal.text, terminal.text
            assert 'idle ' not in terminal.text and 'done ' not in terminal.text and 'other ' not in terminal.text, terminal.text
        (evidence / 'compact-six-active-screen.txt').write_text(terminals[1].text + '\n')
        stages.append('six active workers show three task-first rows plus +3 more, with elapsed, unknown remote tokens and actual Sol high/Astra medium below shortcuts')
        for index, terminal in enumerate(terminals):
            assert f'Workers {len(servers[1 - index].workers)}' not in terminal.text, terminal.text
            os.write(terminal.master, f'draft-{index}'.encode())
        wait(terminals, lambda: all(f'draft-{index}' in terminal.text for index, terminal in enumerate(terminals)), 'composer unavailable')
        stages.append('two simultaneous independent roots and editable composers')

        servers[0].hold_metadata = True
        wait(terminals, servers[0].metadata_pending.is_set, 'metadata poll did not occur')
        os.write(terminals[0].master, b'-refresh')
        wait(terminals, lambda: 'draft-0-refresh' in terminals[0].text, 'refresh blocked input', 2)
        servers[0].release_metadata.set()
        servers[0].hold_metadata = False
        stages.append('input remains editable during delayed metadata RPC')

        active_worker = servers[1].workers[0]
        active_worker['status'] = {'type': 'idle'}
        servers[1].turns[active_worker['id']][0].update(status='completed', completedAt=int(time.time()))
        wait(terminals, lambda: 'Workers 5' in terminals[1].text and '+2 more' in terminals[1].text and 'worker 1 ·' not in terminals[1].text and 'worker 4 ·' in terminals[1].text, 'completed worker did not disappear and promote hidden active worker')
        (evidence / 'compact-promoted-screen.txt').write_text(terminals[1].text + '\n')
        stages.append('completion removes its row and promotes a hidden active worker')
        for worker in servers[1].workers[1:-1]:
            worker['status'] = {'type': 'idle'}
            servers[1].turns[worker['id']][0].update(status='completed', completedAt=int(time.time()))
        wait(terminals, lambda: 'Workers 1' in terminals[1].text and 'worker 6 ·' in terminals[1].text and '+2 more' not in terminals[1].text, 'remaining active worker did not replace completed rows')
        last_worker = servers[1].workers[-1]
        last_worker['status'] = {'type': 'idle'}
        servers[1].turns[last_worker['id']][0].update(status='completed', completedAt=int(time.time()))
        wait(terminals, lambda: 'Workers' not in terminals[1].text and 'worker 6 ·' not in terminals[1].text, 'healthy HUD did not disappear after the last worker finished')
        os.write(terminals[1].master, b'-quiet')
        wait(terminals, lambda: 'draft-1-quiet' in terminals[1].text, 'hidden HUD blocked the composer')
        assert 'Workers 2' in terminals[0].text and 'draft-0-refresh' in terminals[0].text, terminals[0].text
        (evidence / 'compact-all-finished-screen.txt').write_text(terminals[1].text + '\n')
        stages.append('last completion hides the entire strip while both composers remain editable')
        for worker in servers[1].workers:
            worker['status'] = {'type': 'active', 'activeFlags': []}
            servers[1].turns[worker['id']][0].update(status='inProgress', startedAt=int(time.time()), completedAt=None)
        wait(terminals, lambda: 'Workers 6' in terminals[1].text and '+3 more' in terminals[1].text and 'worker 1 ·' in terminals[1].text and 'draft-1-quiet' in terminals[1].text, 'new runtime activity did not restore compact HUD')
        stages.append('later activity restores the compact strip without losing the draft')

        grouped_server, grouped_terminal = servers[1], terminals[1]
        for index, member in enumerate(('render', 'test', 'review')):
            worker = grouped_server.workers[index]
            worker['source']['subAgent']['thread_spawn']['agent_path'] = f'/root/hud_styling__b1__{member}'

        def group_line():
            return next((line.strip() for line in grouped_terminal.screen.display if 'hud styling (' in line), '')

        wait(terminals, lambda: 'Workers 6' in grouped_terminal.text and '(3 agents)' in group_line() and '+1 more' in grouped_terminal.text, 'explicit cohort did not replace three individual rows')
        assert group_line().endswith('mixed') and '? tok' in group_line(), group_line()
        assert 'worker 1 ·' not in grouped_terminal.text and 'worker 2 ·' not in grouped_terminal.text and 'worker 3 ·' not in grouped_terminal.text, grouped_terminal.text
        initial_age = re.search(r' · (\d+):(\d{2}) · ', group_line())
        initial_age = int(initial_age[1]) * 60 + int(initial_age[2])
        assert initial_age >= 120, group_line()
        (evidence / 'group-three-screen.txt').write_text(grouped_terminal.text + '\n')
        replies_before_resize = grouped_terminal.resize(24, 80)
        wait(terminals, lambda: grouped_terminal.resized_frame(replies_before_resize) and '(3 agents)' in group_line() and 'draft-1-quiet' in grouped_terminal.text, 'group resize lost count or draft')
        os.write(grouped_terminal.master, b'-group')
        wait(terminals, lambda: 'draft-1-quiet-group' in grouped_terminal.text, 'group HUD blocked composer')
        replies_before_resize = grouped_terminal.resize(32, 120)
        wait(terminals, lambda: grouped_terminal.resized_frame(replies_before_resize) and '(3 agents)' in group_line(), 'normal resize lost group')
        for index, remaining in enumerate((2, 1, 0)):
            worker = grouped_server.workers[index]
            worker['status'] = {'type': 'idle'}
            grouped_server.turns[worker['id']][0].update(status='completed', completedAt=int(time.time()))
            suffix = '' if remaining == 1 else 's'
            wait(terminals, lambda: f'Workers {remaining + 3}' in grouped_terminal.text and (f'({remaining} agent{suffix})' in group_line() if remaining else not group_line()), f'group completion failed to show {remaining} remaining members')
            if remaining:
                assert sum('hud styling (' in line for line in grouped_terminal.screen.display) == 1, grouped_terminal.text
                age = re.search(r' · (\d+):(\d{2}) · ', group_line())
                assert int(age[1]) * 60 + int(age[2]) >= initial_age, group_line()
                assert '? tok' in group_line(), group_line()
            else:
                assert all(f'worker {n} ·' in grouped_terminal.text for n in (4, 5, 6)), grouped_terminal.text
            (evidence / f'group-{remaining}-screen.txt').write_text(grouped_terminal.text + '\n')
        for worker in grouped_server.workers[3:]:
            worker['status'] = {'type': 'idle'}
            grouped_server.turns[worker['id']][0].update(status='completed', completedAt=int(time.time()))
        wait(terminals, lambda: 'Workers' not in grouped_terminal.text and 'draft-1-quiet-group' in grouped_terminal.text, 'group and independent cohort did not collapse after completion')
        stages.append('explicit three-member cohort reduces 3→2→1→hidden without duplicate rows or shrinking dispatch elapsed; independent cohort stays separate, group resize/input preserve draft, final strip collapses')
        for worker in grouped_server.workers:
            worker['status'] = {'type': 'active', 'activeFlags': []}
            grouped_server.turns[worker['id']][0].update(status='inProgress', startedAt=int(time.time()), completedAt=None)
        wait(terminals, lambda: 'Workers 6' in grouped_terminal.text and '(3 agents)' in group_line(), 'group reactivation did not restore strip')

        replies_before_resize = terminals[0].resize(10, 50)
        wait(terminals, lambda: terminals[0].resized_frame(replies_before_resize) and 'draft-0-refresh' in terminals[0].text, 'small terminal did not finish repainting')
        os.write(terminals[0].master, b'-small')
        wait(terminals, lambda: 'draft-0-refresh-small' in terminals[0].text, 'small terminal lost composer')
        assert 'Workers' in terminals[0].text, terminals[0].text
        (evidence / 'small-screen.txt').write_text(terminals[0].text + '\n')
        replies_before_resize = terminals[0].resize(32, 120)
        wait(terminals, lambda: terminals[0].resized_frame(replies_before_resize) and 'Workers 2' in terminals[0].text and 'draft-0-refresh-small' in terminals[0].text, 'normal resize did not restore HUD')
        stages.append('small and normal PTY resize with preserved draft')

        servers[0].reject_turn_metadata = True
        wait(terminals, lambda: 'Workers stale' in terminals[0].text and 'counts unavailable' in terminals[0].text, 'partial metadata failure did not mark snapshot stale')
        assert 'draft-0-refresh-small' in terminals[0].text, terminals[0].text
        (evidence / 'metadata-error-screen.txt').write_text(terminals[0].text + '\n')
        servers[0].reject_turn_metadata = False
        wait(terminals, lambda: 'Workers 2' in terminals[0].text and 'Workers stale' not in terminals[0].text, 'metadata recovery did not restore counts')
        stages.append('partial metadata RPC failure marks counts stale and recovers')

        servers[0].disconnect()
        wait(terminals, lambda: 'Workers stale' in terminals[0].text and 'counts unavailable' in terminals[0].text, 'disconnect did not mark counts stale')
        os.write(terminals[0].master, b'-offline')
        wait(terminals, lambda: 'draft-0-refresh-small-offline' in terminals[0].text, 'disconnect blocked draft editing')
        assert 'Workers 6' in terminals[1].text and 'draft-1-quiet' in terminals[1].text, terminals[1].text
        (evidence / 'stale-screen.txt').write_text(terminals[0].text + '\n')
        servers[0].available.set()
        wait(terminals, lambda: 'Workers 2' in terminals[0].text and 'Workers stale' not in terminals[0].text and 'draft-0-refresh-small-offline' in terminals[0].text, 'reconnect failed to recover HUD and draft', 30)
        stages.append('stale disconnect and automatic reconnect without cross-pane effect')

        for server in servers:
            assert not server.errors, server.errors
            assert not any(request.get('method') in ('turn/start', 'turn/steer') for request in server.requests), server.requests
        stages.append('zero turn/start or turn/steer requests and zero model calls')
        stages.extend(tmux_smoke(binary, root, evidence))
    finally:
        for home in Path(temporary.name).glob('*home-*'):
            for log in home.glob('log/*'):
                if log.is_file():
                    shutil.copy2(log, evidence / f'{home.name}-{log.name}')
        for index, terminal in enumerate(terminals):
            (evidence / f'pane-{index}.ansi').write_bytes(terminal.output)
            (evidence / f'pane-{index}-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        for index, server in enumerate(servers):
            server.close()
            (evidence / f'rpc-{index}.json').write_text(json.dumps(server.requests, indent=2) + '\n')
            (evidence / f'fixture-errors-{index}.json').write_text(json.dumps(server.errors, indent=2) + '\n')
        temporary.cleanup()
    report = {'binary': str(binary), 'passed': stages, 'ui_turn_start_requests': 0, 'ui_turn_steer_requests': 0, 'cursor_replies': [terminal.cursor_replies for terminal in terminals], 'model_calls': 0, 'terminal_parser': 'pyte 0.8.2', 'tmux_required': False}
    (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, default=Path('/tmp/codex-hud-native-smoke-evidence'))
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve())
