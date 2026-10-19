"""Real native TUI acceptance with isolated homes and loopback protocol fixtures.

Setup: python3 -m venv /tmp/codex-plus-native-smoke-venv
       /tmp/codex-plus-native-smoke-venv/bin/pip install pyte==0.8.2
Run:   /tmp/codex-plus-native-smoke-venv/bin/python tests/native_smoke.py BINARY
"""

import argparse
from collections import Counter
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
import sys
import tempfile
import termios
import time

import pyte
from wcwidth import wcwidth

from fixtures.native_server import AppServer
from fixtures.background_provider import ResponsesProvider
from fixtures.advisor_provider import AdvisorProvider
from fixtures.foreground_advisor_provider import ForegroundAdvisorProvider
from fixtures.worker_shell_wait_provider import wait_script
from test_background_integration import REPO, RecordingClient, stop, wait as wait_server


class FixtureScreen(pyte.Screen):
    @property
    def display(self):
        # pyte 0.8.2 leaves empty wide-character stubs after a partial overwrite.
        rows = []
        for y in range(self.lines):
            line, skip = [], False
            for x in range(self.columns):
                if skip:
                    skip = False
                    continue
                text = self.buffer[y][x].data or ' '
                assert sum(map(wcwidth, text[1:])) == 0
                skip = wcwidth(text[0]) == 2
                line.append(text)
            rows.append(''.join(line))
        return rows


class Terminal:
    def __init__(self, binary, home, cwd, endpoint=None, parent=None):
        self.master, slave = pty.openpty()
        self.screen = FixtureScreen(120, 32)
        self.cursor_replies, self.cursor_reply_offset = 0, 0
        self.screen.write_process_input = self.reply
        self.stream = pyte.ByteStream(self.screen)
        self.output, self.answered = bytearray(), set()
        self.resize(32, 120, notify=False)
        env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE', 'STY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY')}
        env.update(CODEX_HOME=str(home), TERM='xterm-256color', TERM_PROGRAM='kitty', OPENAI_API_KEY='isolated-fixture-only')
        arguments = ([str(binary), '--remote', endpoint] if endpoint is not None
                     else [str(binary), 'resume', parent] if parent is not None else [str(binary)])
        self.process = subprocess.Popen(arguments + ['--yolo', '-C', str(cwd)], stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
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


def assert_no_park_records(home):
    # Goal input admission can create the ledger without registering shell work.
    database = home / 'codex-plus/background.sqlite'
    if database.exists():
        with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
            for table in ('parks', 'jobs', 'callbacks'):
                assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0, table


def composer_ready(text):
    return any(line.lstrip().startswith('›') for line in text.splitlines())


def worker_rows(text):
    return [line.strip() for line in text.splitlines()
            if re.fullmatch(r'[^·›>]+ \([^()·›>]*\)(?: \[[^\]·›>]*\])? · '
                            r'.+ · (?:\d+:\d{2}|\?)(?: · (?:\?|[\d,.]+[kKmMbBtT]?)'
                            r'(?: (?:t|to|tok))?)?(?: ·)?', line.strip())]


def shell_rows(text):
    return [line.strip() for line in text.splitlines() if line.strip().startswith('Background · ')]


def advisor_rows(text):
    return [line.strip() for line in text.splitlines() if line.strip().startswith('Advisor · ')]


def advisor_transcript_rows(text):
    return [line.strip() for line in text.splitlines()
            if re.fullmatch(r'• (?:Advisor on|Advised on|Advisor failed on) .+ · \d+:\d{2}',
                            line.strip())]


def assert_compact_hud(text, *, rows=None, idle=True):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    activities = worker_rows(text) + shell_rows(text) + advisor_rows(text)
    alerts = [line for line in lines if line.startswith(('Workers stale', 'Workers unavailable',
              'Shells stale', 'Shells unavailable', 'Background status unavailable', 'Watcher ', 'Wake pending', 'Wake delivery ', 'Restart report '))]
    overflow = [line for line in lines if re.match(r'^\+\d+ more$', line)]
    hud = activities + alerts + overflow
    assert len(hud) <= 3, f'HUD exceeded three total rows\n{text}'
    if rows is not None:
        assert len(hud) == rows, f'expected {rows} HUD rows, got {len(hud)}\n{text}'
    assert len(alerts) <= 1, f'alerts were not combined\n{text}'
    assert not any(re.match(r'^Workers \d|^Shells \d', line) for line in lines), text
    assert all(' · active · ' not in line and ' · running · ' not in line and ' · job ' not in line
               for line in activities), text
    assert 'for shortcuts' not in text and 'for agents' not in text, text
    if hud:
        composer = max(index for index, line in enumerate(lines) if line.startswith('›'))
        assert all(composer < lines.index(line) for line in hud), text
    if idle:
        assert any(line.startswith('›') for line in lines[-6:]), f'idle composer fell outside last six nonempty lines\n{text}'
    return hud


def write_model_catalog(home, *, astra=False):
    """Keep baseline cases unavailable; opt advisor cases into an explicit catalog."""
    models = []
    for slug in (['gpt-6.1-sol', 'gpt-6-astra'] if astra else ['gpt-6.1-sol']):
        models.append(dict(slug=slug, display_name=slug, description='loopback fixture model',
            default_reasoning_level='high',
            supported_reasoning_levels=[{'effort': 'high', 'description': 'High'},
                                        {'effort': 'xhigh', 'description': 'Extra high'}],
            shell_type='shell_command', visibility='list', minimal_client_version=[0, 1, 0],
            supported_in_api=True, priority=0, upgrade=None,
            model_messages={'instructions_template': 'Read fixture evidence.',
                            'approvals': None, 'auto_review': None, 'permissions': None},
            support_verbosity=False, default_verbosity=None, apply_patch_tool_type=None,
            truncation_policy={'mode': 'bytes', 'limit': 10000},
            supports_image_detail_original=False, context_window=272000,
            max_context_window=272000, experimental_supported_tools=[]))
    path = home / 'fixture-models.json'
    path.write_text(json.dumps({'models': models}))
    return f'model_catalog_json={json.dumps(str(path))}\n'


def write_health(home, parent, state, *, jobs=0, uncertain=0, reports=0, age=0):
    directory = home / 'codex-plus'
    directory.mkdir(exist_ok=True)
    target = directory / f'{parent}.status.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 1, 'parent': parent,
        'updated_at': time.time() - age, 'state': state,
        'jobs': jobs, 'uncertain': uncertain, 'reports': reports}))
    temporary.replace(target)


def token_usage_record(thread, turn, total, lifetime):
    def usage(tokens):
        return dict(input_tokens=tokens, cached_input_tokens=0, output_tokens=0,
                    reasoning_output_tokens=0, total_tokens=tokens)
    return dict(timestamp='2026-10-17T00:00:00Z', type='token_usage_record', payload=dict(
        thread_id=thread, turn_id=turn, session_id=thread, root_turn_id=turn,
        response_id=f'fixture-{turn}', usage=usage(total),
        turn_token_usage=usage(total), thread_token_usage=usage(lifetime)))


def shell_hud_smoke(binary, root, evidence):
    """Remote snapshots are authoritative even when their PIDs are not local."""
    terminals, servers = [], []

    def rows(terminal):
        return shell_rows(terminal.text)

    def age(line):
        match = re.search(r'seen (\d+):(\d{2})', line)
        assert match, line
        return int(match[1]) * 60 + int(match[2])

    try:
        for index, workers in enumerate((0, 2)):
            home, workspace = root / f'shell-home-{index}', root / f'shell-workspace-{index}'
            home.mkdir()
            workspace.mkdir()
            (home / 'config.toml').write_text(f'model="gpt-6-sol"\nmodel_provider="openai"\nsuppress_unstable_features_warning=true\nanalytics.enabled=false\nfeatures.daemon_auto_start=false\n[projects."{workspace}"]\ntrust_level="trusted"\n')
            (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
            server = AppServer(f'00000000-0000-0000-000{index + 4}-000000000001', workspace, workers)
            server.shells = [server.shell(700 + index, f'remote-root-{index}-held')]
            servers.append(server)
            terminals.append(Terminal(binary, home, workspace, server.url))
            assert_no_park_records(home)
        first, second = terminals
        wait(terminals, lambda: rows(first) and 'remote-root-0-held' in first.text and
             'remote-root-1-held' in second.text and len(worker_rows(second.text)) == 2 and
             all('Workers stale' not in terminal.text and 'Shells stale' not in terminal.text
                 for terminal in terminals),
             'unregistered shell-only and mixed worker rows did not render', 30)
        assert not worker_rows(first.text) and len(worker_rows(second.text)) == 2, '\n'.join(t.text for t in terminals)
        assert 'remote-root-1-held' not in first.text and 'remote-root-0-held' not in second.text
        assert_compact_hud(first.text, rows=1)
        assert_compact_hud(second.text, rows=3)
        for index, terminal in enumerate(terminals):
            os.write(terminal.master, f'shell-draft-{index}'.encode())
        wait(terminals, lambda: all(f'shell-draft-{i}' in t.text for i, t in enumerate(terminals)), 'shell HUD blocked drafts')
        assert len(rows(second)) == 1 and len(worker_rows(second.text)) == 2, second.text
        assert rows(first)[0].startswith('Background · remote-root-0-held · seen '), rows(first)
        assert '987654' not in first.text and '345678' not in first.text and '12.5' not in first.text, first.text
        initial_age = age(rows(first)[0])
        wait(terminals, lambda: age(rows(first)[0]) >= initial_age + 2, 'observed shell elapsed did not advance')
        servers[0].shells[0]['itemId'] = 'replacement-item-same-process'
        wait(terminals, lambda: age(rows(first)[0]) <= 1, 'same process with new item inherited old observed elapsed')
        wait(terminals, lambda: age(rows(first)[0]) >= 2, 'replacement shell observation timer did not advance')
        servers[0].shells[0]['processId'] = '799'
        wait(terminals, lambda: age(rows(first)[0]) <= 1, 'new process with same item inherited old observed elapsed')
        (evidence / 'shell-only-screen.txt').write_text(first.text + '\n')

        servers[0].shells.extend(servers[0].shell(710 + i, f'paginated-shell-{i}') for i in range(3))
        servers[1].shells.extend(servers[1].shell(720 + i, f'mixed-shell-{i}') for i in range(3))
        wait(terminals, lambda: '+2 more' in first.text and '+4 more' in second.text,
             'paginated shell counts or mixed overflow did not render')
        assert len(rows(first)) == 2, first.text
        details = len(rows(second)) + len(worker_rows(second.text))
        assert details == 2 and rows(second) and worker_rows(second.text), second.text
        assert_compact_hud(first.text, rows=3)
        assert_compact_hud(second.text, rows=3)
        assert all(any(r.get('method') == 'thread/backgroundTerminals/list' and r['params'].get('cursor') == '2'
                       for r in server.requests) for server in servers), 'second shell page was never read'
        (evidence / 'shell-mixed-screen.txt').write_text(second.text + '\n')

        servers[1].reject_shell_cursor = '2'
        wait(terminals, lambda: 'shells stale' in second.text.lower(), 'partial shell page failure did not mark shells stale')
        assert worker_rows(second.text) and 'Workers stale' not in second.text and 'shell-draft-1' in second.text, second.text
        assert all(' · stale · seen ?' in line for line in rows(second)), second.text
        assert '+5 more' in second.text, second.text
        assert_compact_hud(second.text, rows=3)
        assert '+2 more' in first.text, first.text
        (evidence / 'shell-error-screen.txt').write_text(second.text + '\n')
        servers[1].reject_turn_metadata = True
        wait(terminals, lambda: 'Workers stale' in second.text and 'Shells stale' in second.text,
             'simultaneous worker and shell errors lost one warning')
        assert_compact_hud(second.text, rows=3)
        assert '+5 more' in second.text and len(worker_rows(second.text)) == 1, second.text
        (evidence / 'mixed-alert-overflow-screen.txt').write_text(second.text + '\n')
        servers[1].reject_turn_metadata = False
        servers[1].reject_shell_cursor = None
        wait(terminals, lambda: '+4 more' in second.text and 'shells stale' not in second.text.lower(), 'shell failure did not recover')

        servers[0].shell_next_cursor = '0'
        wait(terminals, lambda: 'shells stale' in first.text.lower(), 'cyclic shell pagination was accepted as fresh')
        assert rows(first) and all(' · stale · seen ?' in line for line in rows(first)), first.text
        assert_compact_hud(first.text, rows=3)
        (evidence / 'shell-alert-overflow-screen.txt').write_text(first.text + '\n')
        servers[0].shell_next_cursor = None
        wait(terminals, lambda: '+2 more' in first.text and 'stale' not in first.text, 'cyclic pagination did not recover')
        unsafe_command = 'printf 界界界 \x1b[31mcontrol\x1b[0m\nnext\r\ttab\x1b]0;shell-control-title\x07'
        servers[0].shells = [servers[0].shell(730, unsafe_command)]
        wait(terminals, lambda: len(rows(first)) == 1 and 'Background · Shell task ·' in first.text,
             'opaque shell command did not use a compact fallback')
        assert '\x1b' not in first.text and not any(line.strip() in ('next', 'tab') for line in first.screen.display), first.text
        assert b'\x1b[31mcontrol' not in first.output and b'\x1b]0;shell-control-title\x07' not in first.output, 'shell command injected terminal controls'
        servers[0].shells = [servers[0].shell(731,
            '# codex-plus-shell: background\n# codex-plus-task: 检查界界界\n' + unsafe_command)]
        wait(terminals, lambda: len(rows(first)) == 1 and '检查界界界' in first.text,
             'Unicode task topic did not render')
        replies = first.resize(10, 50)
        wait(terminals, lambda: first.resized_frame(replies) and rows(first) and 'shell-draft-0' in first.text, 'narrow shell row lost header or draft')
        os.write(first.master, b'-narrow')
        wait(terminals, lambda: 'shell-draft-0-narrow' in first.text, 'narrow shell HUD blocked draft editing')
        (evidence / 'shell-unicode-narrow-screen.txt').write_text(first.text + '\n')
        replies = first.resize(32, 120)
        wait(terminals, lambda: first.resized_frame(replies) and '界' in first.text, 'shell row did not survive normal resize')
        servers[0].reject_shells = True
        wait(terminals, lambda: 'shells stale' in first.text.lower(), 'shell-only RPC failure hid uncertainty')
        servers[0].reject_shells = False
        servers[0].shells.clear()
        servers[1].shells.clear()
        wait(terminals, lambda: 'Shells' not in first.text and not rows(first) and not rows(second) and
             'shells' not in second.text and len(worker_rows(second.text)) == 2, 'successful empty snapshots did not remove shells')
        assert all(marker not in first.text.lower() for marker in ('succeeded', 'completed', 'exit 0')), first.text
        assert 'shell-draft-0-narrow' in first.text and 'shell-draft-1' in second.text
        (evidence / 'shell-complete-screen.txt').write_text(first.text + '\n' + second.text + '\n')
        for server in servers:
            assert not server.errors, server.errors
            assert not any(r.get('method') in ('turn/start', 'turn/steer', 'command/exec') for r in server.requests), server.requests
        return ['unregistered remote shell-only and mixed worker/shell strips; three total rows including overflow and alerts; compact background labels and observed elapsed; item identity reset; parent isolation; shell-only stale failures and recovery; Unicode/control-safe narrow drafts; successful disappearance without success claim; zero UI turn or execution requests']
    finally:
        for index, terminal in enumerate(terminals):
            (evidence / f'shell-pane-{index}.ansi').write_bytes(terminal.output)
            (evidence / f'shell-pane-{index}-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        for index, server in enumerate(servers):
            server.close()
            (evidence / f'shell-rpc-{index}.json').write_text(json.dumps(server.requests, indent=2) + '\n')
            (evidence / f'shell-fixture-errors-{index}.json').write_text(json.dumps(server.errors, indent=2) + '\n')


def local_shell_smoke(binary, root, evidence, task_topic=None):
    """The real embedded app-server retains a yielded command after its turn ends."""
    home, workspace = root / 'local-shell-home', root / 'local-shell-workspace'
    home.mkdir(mode=0o700)
    workspace.mkdir()
    release, finished = workspace / 'release', workspace / 'finished'
    command = (f'deadline=$((SECONDS+60)); while [ ! -e {shlex.quote(str(release))} ] && '
               f'(( SECONDS < deadline )); do sleep 0.1; done; '
               f'test -e {shlex.quote(str(release))} || exit 124; '
               f'printf canonical-shell-completion > {shlex.quote(str(finished))}')
    if task_topic:
        command = f'# codex-plus-shell: background\n# codex-plus-task: {task_topic}\n{command}'
    provider = ResponsesProvider(command)
    terminal = None
    (home / 'config.toml').write_text(write_model_catalog(home) + f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
suppress_unstable_features_warning=true
check_for_update_on_startup=false
analytics.enabled=false
features.daemon_auto_start=false
features.unified_exec=true
features.shell_tool=true
features.plugins=false
tui.status_line=["thread-name"]
[projects."{workspace}"]
trust_level="trusted"
[model_providers.isolated]
name="loopback local shell acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')

    def events():
        return [json.loads(line) for path in (home / 'sessions').glob('**/*.jsonl')
                for line in path.read_text().splitlines() if line]

    def completed_turns():
        return sum(event.get('type') == 'event_msg' and
                   event.get('payload', {}).get('type') in ('task_complete', 'turn_complete') for event in events())

    try:
        terminal = Terminal(binary, home, workspace)
        wait([terminal], lambda: composer_ready(terminal.text),
             'private local shell TUI did not initialize', 30)
        # Naming the fixture root prevents an unrelated automatic title model call.
        os.write(terminal.master, b'/rename local-shell-acceptance')
        wait([terminal], lambda: '/rename local-shell-acceptance' in terminal.text,
             'private local shell rename did not finish rendering')
        os.write(terminal.master, b'\r')
        wait([terminal], lambda: 'local-shell-acceptance' in terminal.text and
             '/rename local-shell-acceptance' not in terminal.text, 'private local shell root was not named')
        assert provider.count == 0, 'fixture rename unexpectedly called a model'
        os.write(terminal.master, b'Start the finite held command and end this turn.')
        wait([terminal], lambda: 'Start the finite held command and end this turn.' in terminal.text,
             'private local shell prompt did not finish rendering')
        os.write(terminal.master, b'\r')
        wait([terminal], lambda: provider.count == 2, 'real local command did not yield to loopback final response', 30)
        assert not finished.exists(), 'finite shell completed before its release'
        provider.final_gate.set()
        wait([terminal], lambda: completed_turns() == 1 and shell_rows(terminal.text) and 'seen ' in terminal.text,
             'real idle local root did not show its still-running background shell', 30)
        assert_no_park_records(home)
        if task_topic:
            assert any(row.startswith(f'Background · {task_topic} · seen ')
                       for row in shell_rows(terminal.text)), terminal.text
        assert provider.count == 2, f'expected two fixture model calls, got {provider.count}; see local-shell-provider-requests.json'
        assert not finished.exists(), 'command stopped before editable idle interval'
        assert_compact_hud(terminal.text, rows=1)
        os.write(terminal.master, b'local-shell-idle-draft')
        wait([terminal], lambda: 'local-shell-idle-draft' in terminal.text and shell_rows(terminal.text),
             'idle real-shell HUD blocked draft editing')
        requests_before = provider.count
        observed_before = time.monotonic()
        wait([terminal], lambda: time.monotonic() - observed_before >= 2 and shell_rows(terminal.text),
             'real command was not visible for an idle polling interval')
        assert not finished.exists() and completed_turns() == 1 and provider.count == requests_before == 2
        (evidence / 'local-shell-idle-screen.txt').write_text(terminal.text + '\n')
        release.touch()
        wait([terminal], lambda: finished.exists() and not shell_rows(terminal.text),
             'real shell row did not disappear after actual process completion', 20)
        assert finished.read_text() == 'canonical-shell-completion'
        assert 'local-shell-idle-draft' in terminal.text and provider.count == requests_before == 2
        assert completed_turns() == 1, 'shell polling or completion started an extra turn'
        if task_topic:
            commands = [json.loads(event['payload']['arguments']).get('cmd')
                        for event in events() if event.get('payload', {}).get('type') == 'function_call'
                        and event['payload'].get('name') == 'exec_command']
            assert command in commands, 'HUD topic replaced the stored full command'
        (evidence / 'local-shell-complete-screen.txt').write_text(terminal.text + '\n')
        (evidence / 'local-shell-rollout.json').write_text(json.dumps(events(), indent=2) + '\n')
        return ['real private local embedded app-server: one loopback tool turn, yielded finite shell survives completed turn, row and editable idle draft without park registration, actual command completion removes row, zero extra model calls or turns']
    finally:
        release.touch()
        if terminal:
            (evidence / 'local-shell.ansi').write_bytes(terminal.output)
            (evidence / 'local-shell-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        for log in home.glob('log/*'):
            if log.is_file():
                shutil.copy2(log, evidence / f'local-shell-{log.name}')
        (evidence / 'local-shell-provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        provider.close()


def local_health_smoke(binary, root, evidence):
    """Resume real idle local roots; health-only changes must never call a model."""
    home, workspace = root / 'health-home', root / 'health-workspace'
    home.mkdir(mode=0o700)
    workspace.mkdir()
    provider = ResponsesProvider(None)
    provider.final_gate.set()
    native = rpc = None
    terminals = []
    native_log = (evidence / 'health-native.stderr').open('w')
    env = {key: value for key, value in os.environ.items()
           if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
    env.update(CODEX_HOME=str(home), RUST_LOG='warn')
    (home / 'config.toml').write_text(write_model_catalog(home) + f'''model="gpt-6.1-sol"
model_provider="isolated"
suppress_unstable_features_warning=true
check_for_update_on_startup=false
analytics.enabled=false
features.daemon_auto_start=false
features.plugins=false
[projects."{workspace}"]
trust_level="trusted"
[model_providers.isolated]
name="loopback health acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
    try:
        native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
            stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log, start_new_session=True)
        socket = home / 'app-server-control' / 'app-server-control.sock'
        wait_server(socket.exists, 'private health app-server did not start')
        rpc = RecordingClient(socket)
        parents = [rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
            'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id'] for _ in range(2)]
        # Native resume requires a saved rollout; seed it before measuring health UI calls.
        for index, parent in enumerate(parents):
            rpc.call('turn/start', {'threadId': parent,
                'input': [{'type': 'text', 'text': 'Seed isolated idle root.', 'text_elements': []}]})
            wait_server(lambda: provider.count == index + 1, 'isolated seed response did not reach loopback provider')
            wait_server(lambda: rpc.call('thread/read', {'threadId': parent})['thread']['status']['type'] == 'idle',
                        'isolated seed turn did not finish')
        requests_before_ui = provider.count
        assert requests_before_ui == 2, provider.requests
        write_health(home, parents[0], 'waiting', jobs=1)
        write_health(home, parents[1], 'idle')
        terminals = [Terminal(binary, home, workspace, parent=parent) for parent in parents]
        first, second = terminals
        wait(terminals, lambda: all(composer_ready(t.text) for t in terminals) and
             'Watcher' not in first.text and 'Workers' not in first.text and 'Watcher' not in second.text,
             'healthy nonempty waiting health did not remain hidden', 30)
        assert 'Workers' not in first.text and 'Watcher' not in second.text, '\n'.join(t.text for t in terminals)
        for index, terminal in enumerate(terminals):
            os.write(terminal.master, f'health-draft-{index}'.encode())
        wait(terminals, lambda: all(f'health-draft-{index}' in terminal.text
             for index, terminal in enumerate(terminals)), 'local health blocked draft editing')
        assert_compact_hud(first.text, rows=0)
        (evidence / 'health-waiting-screen.txt').write_text(first.text + '\n')
        write_health(home, parents[0], 'waiting', jobs=0)
        wait(terminals, lambda: 'Wake pending' in first.text, 'waiting zero lost its pending distinction')
        assert_compact_hud(first.text, rows=1)
        (evidence / 'health-pending-zero-screen.txt').write_text(first.text + '\n')
        write_health(home, parents[0], 'waiting', jobs=1)
        wait(terminals, lambda: 'Wake pending' not in first.text and 'Watcher' not in first.text,
             'healthy waiting failed to hide the pending-zero warning')
        assert_compact_hud(first.text, rows=0)
        write_health(home, parents[0], 'waiting', jobs=1, age=20)
        wait(terminals, lambda: 'Watcher unavailable' in first.text, 'old heartbeat was not shown as unavailable')
        assert_compact_hud(first.text, rows=1)
        assert 'Watcher' not in second.text, second.text
        (evidence / 'health-stale-screen.txt').write_text(first.text + '\n')
        write_health(home, parents[0], 'uncertain', uncertain=1)
        wait(terminals, lambda: 'Wake delivery uncertain' in first.text, 'uncertain callback did not render')
        replies_before_resize = first.resize(10, 50)
        wait(terminals, lambda: first.resized_frame(replies_before_resize) and
             'Wake delivery uncertain' in first.text and 'health-draft-0' in first.text,
             'narrow terminal lost health warning or draft')
        os.write(first.master, b'-narrow')
        wait(terminals, lambda: 'health-draft-0-narrow' in first.text, 'health warning blocked narrow draft editing')
        assert_compact_hud(first.text, rows=1)
        (evidence / 'health-uncertain-narrow-screen.txt').write_text(first.text + '\n')
        first.resize(32, 120)
        write_health(home, parents[0], 'report', reports=1)
        wait(terminals, lambda: 'Restart report ready' in first.text and
             'health-draft-0-narrow' in first.text, 'restart report did not replace uncertain warning')
        assert_compact_hud(first.text, rows=1)
        assert 'Watcher' not in second.text and 'Wake delivery' not in second.text and 'Restart report' not in second.text, second.text
        (evidence / 'health-report-screen.txt').write_text(first.text + '\n')
        write_health(home, parents[0], 'idle')
        wait(terminals, lambda: all(marker not in first.text for marker in
             ('Watcher', 'Wake pending', 'Wake delivery uncertain', 'Restart report ready')) and
             'health-draft-0-narrow' in first.text, 'idle health did not hide warning')
        assert provider.count == requests_before_ui, 'health UI called model: ' + repr(provider.requests)
        for parent in parents:
            assert len(rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']['turns']) == 1, 'health UI created a turn'
        return ['private local app-server: two loopback seed turns, then idle roots, healthy waiting hidden, zero jobs pending visible, stale/uncertain/report/idle health, last-six composer detection, narrow draft editing, parent isolation, zero additional model calls or turns']
    finally:
        for index, terminal in enumerate(terminals):
            (evidence / f'health-pane-{index}.ansi').write_bytes(terminal.output)
            (evidence / f'health-pane-{index}-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        if rpc:
            rpc.close()
        if native:
            stop(native)
        native_log.close()
        for log in home.glob('log/*'):
            if log.is_file():
                shutil.copy2(log, evidence / f'health-{log.name}')
        (evidence / 'health-provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        provider.close()


def advisor_hud_smoke(binary, root, evidence):
    """Local status changes render without consulting a model or changing turns."""
    home, workspace = root / 'advisor-home', root / 'advisor-workspace'
    home.mkdir(mode=0o700)
    workspace.mkdir()
    release = workspace / 'release-shell'
    command = (f'deadline=$((SECONDS+90)); while [ ! -e {shlex.quote(str(release))} ] && '
               '(( SECONDS < deadline )); do sleep 0.1; done; echo advisor-shell-complete')
    fixture_bin = root / 'advisor-fixture' / 'bin'
    fixture_bin.mkdir(parents=True)
    fixture_launcher = fixture_bin / 'codex-plus'
    fixture_calls = workspace / 'advisor-stub-calls'
    fixture_launcher.write_text('#!/bin/bash\n'
                                f'printf "%s\\n" "$1" >> {shlex.quote(str(fixture_calls))}\n'
                                'if [[ "$1" == run && "$2" == -- ]]; then shift 2; exec "$@"; fi\n' +
                                command + '\n')
    fixture_launcher.chmod(0o755)
    provider = AdvisorProvider(command)
    provider.reset('seed')
    native = rpc = None
    terminals = []
    native_log = (evidence / 'advisor-native.stderr').open('w')
    env = {key: value for key, value in os.environ.items()
           if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
    env.update(CODEX_HOME=str(home), RUST_LOG='warn', PATH=str(fixture_bin) + os.pathsep + env['PATH'])
    (home / 'config.toml').write_text(write_model_catalog(home, astra=True) + f'''model="gpt-6.1-sol"
model_provider="isolated"
suppress_unstable_features_warning=true
check_for_update_on_startup=false
analytics.enabled=false
features.daemon_auto_start=false
features.plugins=false
features.unified_exec=true
features.shell_tool=true
[projects."{workspace}"]
trust_level="trusted"
[model_providers.isolated]
name="loopback advisor footer acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')

    def publish(identifier, state, *, model='gpt-6-astra', age=0, **changes):
        now = time.time()
        value = dict(version=1, parent=identifier, model=model, effort='xhigh', state=state,
                     started_at=now - 83, updated_at=now - age,
                     finished_at=None if state == 'running' else now, calls=2,
                     last_duration_secs=None if state == 'running' else 222,
                     error='exec_failed' if state == 'error' else None)
        value.update(changes)
        directory = home / 'codex-plus'
        directory.mkdir(mode=0o700, exist_ok=True)
        temporary = directory / 'advisor-fixture.tmp'
        temporary.write_text(json.dumps(value))
        temporary.chmod(0o600)
        path = directory / f'{identifier}.advisor.json'
        os.replace(temporary, path)
        return path

    def capture(name):
        for index, terminal in enumerate(terminals):
            (evidence / f'advisor-{name}-pane-{index}.txt').write_text(terminal.text + '\n')

    try:
        native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
            stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log, start_new_session=True)
        socket = home / 'app-server-control/app-server-control.sock'
        wait_server(socket.exists, 'private advisor app-server did not start')
        rpc = RecordingClient(socket)
        parents = [rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
            'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id'] for _ in range(2)]
        for index, parent in enumerate(parents):
            rpc.call('turn/start', {'threadId': parent,
                'input': [{'type': 'text', 'text': 'Seed isolated advisor footer root.', 'text_elements': []}]})
            wait_server(lambda: provider.count == index + 1, 'advisor footer seed did not reach loopback')
            wait_server(lambda: rpc.call('thread/read', {'threadId': parent})['thread']['status']['type'] == 'idle',
                        'advisor footer seed did not finish')
        assert provider.count == 2, provider.requests
        provider.reset('success')
        rpc.call('turn/start', {'threadId': parents[0],
            'input': [{'type': 'text', 'text': 'Start the held shell and finish the turn.', 'text_elements': []}]})
        wait_server(lambda: provider.count == 4, 'advisor mixed fixture shell did not yield')
        wait_server(lambda: rpc.call('thread/read', {'threadId': parents[0]})['thread']['status']['type'] == 'idle',
                    'advisor mixed fixture turn did not finish')
        provider.command = '# codex-plus-shell: background\n' + shlex.join([
            str(fixture_launcher), 'run', '--', str(fixture_launcher), 'advisor', '--brief', 'task'])
        provider.reset('success')
        rpc.call('turn/start', {'threadId': parents[0],
            'input': [{'type': 'text', 'text': 'Start the held advisor command fixture and finish.', 'text_elements': []}]})
        wait_server(lambda: provider.count == 6, 'advisor command fixture did not yield')
        wait_server(lambda: fixture_calls.exists() and
                    fixture_calls.read_text().splitlines() == ['run', 'advisor'],
                    'advisor command escaped its private fixture launcher')
        wait_server(lambda: rpc.call('thread/read', {'threadId': parents[0]})['thread']['status']['type'] == 'idle',
                    'advisor command fixture turn did not finish')
        before = provider.count
        terminals = [Terminal(binary, home, workspace, parent=parent) for parent in parents]
        first, second = terminals
        ready = 'Advisor · GPT-6-Astra (xhigh) · ready · 0 calls'
        wait(terminals, lambda: ready in first.text and ready in second.text,
             'explicit Astra catalog did not show zero-call readiness', 30)
        assert not any(advisor_transcript_rows(t.text) for t in terminals), 'resumed old commands invented an advisor transcript'
        for index, terminal in enumerate(terminals):
            os.write(terminal.master, f'advisor-draft-{index}'.encode())
        wait(terminals, lambda: all(f'advisor-draft-{index}' in terminal.text
             for index, terminal in enumerate(terminals)), 'advisor ready row blocked composer')
        wait(terminals, lambda: len(shell_rows(first.text)) == 2, 'advisor fixture shells absent')
        assert_compact_hud(first.text, rows=3)
        capture('ready')
        publish(parents[0], 'running')
        publish(parents[1], 'done', model='gpt-7.2-astra', calls=1)
        wait(terminals, lambda: 'GPT-6-Astra (xhigh) · reviewing · 1:' in first.text and
             'GPT-7.2-Astra (xhigh) · 1 call · last 3:42' in second.text,
             'running and completed status did not remain isolated per pane')
        wait(terminals, lambda: len(shell_rows(first.text)) == 1,
             'active advisor command shell was not coalesced')
        assert 'advisor --brief' not in shell_rows(first.text)[0], first.text
        assert_compact_hud(first.text, rows=2)
        assert 'GPT-7.2-Astra' not in first.text and 'reviewing' not in second.text, capture('isolation')
        initial = advisor_rows(first.text)[0]
        wait(terminals, lambda: advisor_rows(first.text)[0] != initial,
             'advisor elapsed timer did not advance without status writes')
        capture('running')
        publish(parents[0], 'done')
        wait(terminals, lambda: '2 calls · last 3:42' in first.text and len(shell_rows(first.text)) == 2,
             'done count/duration missing or finished advisor suppressed a shell')
        capture('done')
        publish(parents[0], 'error')
        wait(terminals, lambda: 'GPT-6-Astra (xhigh) · error' in first.text, 'advisor error missing')
        capture('error')
        publish(parents[0], 'running', age=20)
        wait(terminals, lambda: 'GPT-6-Astra (xhigh) · unavailable' in first.text,
             'stale advisor heartbeat still looked active')
        capture('stale')
        path = publish(parents[0], 'running', parent=parents[1])
        wait(terminals, lambda: 'Advisor · ? (xhigh) · unavailable' in first.text,
             'mismatched parent status was accepted')
        path.write_text('{invalid')
        observed = time.monotonic()
        wait(terminals, lambda: time.monotonic() - observed >= 1.5 and 'unavailable' in first.text,
             'malformed advisor status was accepted')
        capture('malformed')
        path.unlink()
        os.mkfifo(path)
        os.write(first.master, b'-fifo')
        observed = time.monotonic()
        wait(terminals, lambda: time.monotonic() - observed >= 1.5 and
             'advisor-draft-0-fifo' in first.text and 'unavailable' in first.text,
             'advisor FIFO read blocked composer or status refresh')
        path.unlink()
        path.symlink_to(workspace / 'absent-status')
        observed = time.monotonic()
        wait(terminals, lambda: time.monotonic() - observed >= 1.5 and 'unavailable' in first.text,
             'advisor symlink status was accepted')
        path.unlink()
        publish(parents[0], 'running')
        write_health(home, parents[0], 'uncertain', uncertain=1)
        wait(terminals, lambda: 'Wake delivery uncertain' in first.text and 'reviewing' in first.text,
             'advisor displaced existing warning priority')
        assert_compact_hud(first.text, rows=3)
        first.resize(10, 80)
        os.write(first.master, b'-small')
        wait(terminals, lambda: 'advisor-draft-0-fifo-small' in first.text and 'reviewing' in first.text,
             'advisor warning combination lost narrow composer')
        assert_compact_hud(first.text)
        capture('warning-small')
        assert not any(advisor_transcript_rows(t.text) for t in terminals), 'status refresh invented an advisor transcript'
        assert provider.count == before, 'advisor footer called a model: ' + repr(provider.requests)
        for index, parent in enumerate(parents):
            assert len(rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']['turns']) == (3 if index == 0 else 1)
        return ['advisor explicit Astra catalog: ready zero calls, running timer, done count/duration, error, stale/malformed/mismatched/FIFO/symlink status, full reported model per pane, managed advisor shell coalesces only while reviewing and unrelated shell stays visible, warning/advisor/real-shell share three rows with narrow AP composer, zero extra model calls or turns']
    finally:
        release.touch()
        for index, terminal in enumerate(terminals):
            (evidence / f'advisor-pane-{index}.ansi').write_bytes(terminal.output)
            (evidence / f'advisor-pane-{index}-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        if rpc:
            rpc.close()
        if native:
            stop(native)
        native_log.close()
        (evidence / 'advisor-provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        provider.close()


def advisor_transcript_smoke(binary, root, evidence, cases=None):
    """A real Code Mode command stays live past its initial exec yield."""
    stages = []
    for wrapper, failed in cases or ((False, False), (True, False), (True, True)):
        label = ('wrapper-' if wrapper else 'direct-') + ('failed' if failed else 'done')
        home, workspace = root / f'transcript-{label}-home', root / f'transcript-{label}-work'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        release = workspace / 'release'
        launcher = workspace / 'bin' / 'codex-plus'
        launcher.parent.mkdir()
        # Only the advisor is simulated: the TUI, Code Mode and shell lifecycle are real.
        launcher.write_text('#!' + sys.executable + '\n' + f'''
import json, os, pathlib, sys, time
if sys.argv[1:3] == ['run', '--']:
    os.execv(sys.argv[3], sys.argv[3:])
home = pathlib.Path(os.environ['CODEX_HOME']) / 'codex-plus'
home.mkdir(exist_ok=True)
parent = os.environ['CODEX_THREAD_ID']
path = home / (parent + '.advisor.json')
value = dict(version=1, parent=parent, model='gpt-6-astra', effort='xhigh',
    state='running', started_at=time.time(), updated_at=time.time(), finished_at=None,
    calls=1, last_duration_secs=None, error=None)
while True:
    now = time.time()
    if pathlib.Path({str(release)!r}).exists():
        value.update(state={'error' if failed else 'done'!r}, finished_at=now,
            updated_at=now, last_duration_secs=now-value['started_at'], error={'exec_failed' if failed else None!r})
    if not path.exists() or value['state'] != 'running':
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        temporary.replace(path)
    if value['state'] != 'running':
        print(json.dumps(dict(model='gpt-6-astra', effort='xhigh', verdict='approve')))
        sys.exit({1 if failed else 0})
    time.sleep(.2)
''')
        launcher.chmod(0o755)
        topic = 'wake and cancellation fixes'
        if wrapper:
            script_path = workspace / 'consult.py'
            script_path.write_text(launcher.read_text())
            command = ('# codex-plus-shell: foreground\n# codex-plus-advisor: ' + topic + '\n' +
                       shlex.join([sys.executable, str(script_path)]))
        else:
            command = '# codex-plus-shell: foreground\n' + shlex.join([
                str(launcher), 'run', '--', str(launcher), 'advisor', '--brief', topic])
        script = wait_script(command, budget_ms=45000, outer_ms=50000).replace('yield_time_ms: 250', 'yield_time_ms: 1000')
        if wrapper:
            unrelated = 'bash -c ' + shlex.quote(
                f'until test -e {shlex.quote(str(release))}; do sleep .1; done')
            first_line, rest = script.split('\n', 1)
            script = (first_line + '\nconst unrelated = await tools.exec_command(' +
                      json.dumps(dict(cmd=unrelated, yield_time_ms=1000, max_output_tokens=1000)) +
                      ');\n' + rest)
        provider = ForegroundAdvisorProvider(script)
        terminal = None
        (home / 'config.toml').write_text(write_model_catalog(home, astra=True) + f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
suppress_unstable_features_warning=true
check_for_update_on_startup=false
analytics.enabled=false
features.daemon_auto_start=false
features.unified_exec=true
features.shell_tool=true
features.plugins=false
features.code_mode=true
features.code_mode_host=true
tui.status_line=["thread-name"]
[projects."{workspace}"]
trust_level="trusted"
[model_providers.isolated]
name="loopback advisor transcript acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
        try:
            terminal = Terminal(binary, home, workspace)
            wait([terminal], lambda: composer_ready(terminal.text),
                 'transcript TUI did not initialize', 30)
            os.write(terminal.master, f'/rename transcript-{label}'.encode())
            wait([terminal], lambda: f'/rename transcript-{label}' in terminal.text, 'rename did not render')
            os.write(terminal.master, b'\r')
            wait([terminal], lambda: f'transcript-{label}' in terminal.text and '/rename' not in terminal.text,
                 'transcript root was not named')
            os.write(terminal.master, b'Consult the held advisor.')
            wait([terminal], lambda: 'Consult the held advisor.' in terminal.text, 'prompt did not render')
            os.write(terminal.master, b'\r')
            wait([terminal], lambda: any(f'• Advisor on {topic} · ' in row
                 for row in advisor_transcript_rows(terminal.text)), 'live advisor transcript did not appear', 30)
            first_row = advisor_transcript_rows(terminal.text)
            assert len(first_row) == 1 and 'Astra' not in first_row[0], terminal.text
            status_path, = (home / 'codex-plus').glob('*.advisor.json')
            status_before = status_path.read_bytes()
            started = time.monotonic()
            wait([terminal], lambda: time.monotonic() - started >= 2.2 and
                 advisor_transcript_rows(terminal.text) != first_row, 'local advisor timer did not advance')
            assert provider.count == 1, 'local advisor ticks requested another model response'
            assert status_path.read_bytes() == status_before, 'fixture status changed during local timer check'
            assert len(advisor_transcript_rows(terminal.text)) == 1, terminal.text
            assert '• Advised on ' not in terminal.text, 'initial exec yield prematurely completed advisor row'
            if wrapper:
                assert any(row.startswith('Background · Shell task · seen ')
                           for row in shell_rows(terminal.text)), \
                    'unrelated shell was hidden or relabeled as advisor'
                assert 'Running # codex-plus-advisor:' not in terminal.text, terminal.text
            else:
                assert 'Running ' not in terminal.text, 'advisor kept its ordinary running command header'
            assert any('GPT-6-Astra (xhigh)' in row and 'reviewing' in row
                       for row in advisor_rows(terminal.text)), terminal.text
            assert_compact_hud(terminal.text, idle=False)
            os.write(terminal.master, b'advisor-live-draft')
            wait([terminal], lambda: 'advisor-live-draft' in terminal.text, 'live advisor blocked composer')
            replies = terminal.resize(16, 80)
            wait([terminal], lambda: terminal.resized_frame(replies) and 'advisor-live-draft' in terminal.text and
                 len(advisor_transcript_rows(terminal.text)) == 1, 'resize lost live advisor or draft')
            (evidence / f'advisor-transcript-{label}-live.txt').write_text(terminal.text + '\n')
            # Both completed cells need room when the unrelated shell finishes last.
            replies = terminal.resize(32, 120)
            wait([terminal], lambda: terminal.resized_frame(replies) and 'advisor-live-draft' in terminal.text,
                 'normal advisor viewport did not restore draft')
            release.touch()
            expected = ('• Advisor failed on ' if failed else '• Advised on ') + topic
            wait([terminal], lambda: expected in terminal.text and provider.count == 2,
                 f'{label} advisor transcript did not settle', 20)
            assert len(advisor_transcript_rows(terminal.text)) == 1, terminal.text
            assert '• Advisor on ' not in terminal.text and 'advisor-live-draft' in terminal.text, terminal.text
            settled = advisor_transcript_rows(terminal.text)
            observed = time.monotonic()
            wait([terminal], lambda: time.monotonic() - observed >= 1.5, 'settled frame wait failed')
            assert advisor_transcript_rows(terminal.text) == settled and provider.count == 2, terminal.text
            assert_compact_hud(terminal.text)
            stages.append(f'advisor transcript {label}: real Code Mode exec/wait, live across initial yield, local elapsed ticks, one transcript row, resize and draft preserved, two loopback root responses only')
        finally:
            for path in (home / 'codex-plus').glob('*.advisor.json'):
                shutil.copyfile(path, evidence / f'advisor-transcript-{label}-status.json')
            for path in (home / 'sessions').glob('**/*.jsonl'):
                shutil.copyfile(path, evidence / f'advisor-transcript-{label}-rollout.jsonl')
            for path in (home / 'log').glob('*'):
                if path.is_file():
                    shutil.copyfile(path, evidence / f'advisor-transcript-{label}-{path.name}')
            release.touch()
            if terminal:
                (evidence / f'advisor-transcript-{label}.ansi').write_bytes(terminal.output)
                (evidence / f'advisor-transcript-{label}-screen.txt').write_text(terminal.text + '\n')
                terminal.close()
            (evidence / f'advisor-transcript-{label}-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            provider.close()
    return stages


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
        wait_screen(lambda screens: len(worker_rows(screens[0])) == 2 and len(worker_rows(screens[1])) == 3, 'tmux split HUD rows unavailable', 30)
        for index, pane in enumerate(panes):
            tmux('send-keys', '-l', '-t', pane, f'tmux-draft-{index}')
        wait_screen(lambda screens: all(f'tmux-draft-{index}' in text for index, text in enumerate(screens)), 'tmux composer unavailable')
        tmux('resize-window', '-t', 'native:0', '-x', '110', '-y', '18')
        wait_screen(lambda screens: all(f'tmux-draft-{index}' in text and worker_rows(text) for index, text in enumerate(screens)), 'tmux small resize lost HUD or composer')
        tmux('resize-window', '-t', 'native:0', '-x', '180', '-y', '36')
        wait_screen(lambda screens: len(worker_rows(screens[0])) == 2 and len(worker_rows(screens[1])) == 3, 'tmux normal resize lost rows')
        servers[0].disconnect()
        wait_screen(lambda screens: 'Workers stale' in screens[0] and len(worker_rows(screens[1])) == 3, 'tmux disconnect affected wrong pane')
        tmux('send-keys', '-l', '-t', first, 'offline')
        wait_screen(lambda screens: 'tmux-draft-0offline' in screens[0], 'tmux disconnected composer unavailable')
        servers[0].available.set()
        screens = wait_screen(lambda screens: len(worker_rows(screens[0])) == 2 and 'stale' not in screens[0] and 'tmux-draft-0offline' in screens[0] and len(worker_rows(screens[1])) == 3 and 'tmux-draft-1' in screens[1], 'tmux reconnect failed', 30)
        for index, text in enumerate(screens):
            assert_compact_hud(text, rows=2 + index)
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


def worker_history_smoke(binary, root, evidence):
    """Local current-turn tokens and retained group history need no model calls."""
    home, workspace = root / 'history-home', root / 'history-workspace'
    home.mkdir(mode=0o700)
    workspace.mkdir()
    (home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="openai"\nsuppress_unstable_features_warning=true\nanalytics.enabled=false\nfeatures.daemon_auto_start=false\n')
    (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
    socket = home / 'app-server-control/app-server-control.sock'
    socket.parent.mkdir(mode=0o700)
    server = AppServer('00000000-0000-0000-0008-000000000001', workspace, 33,
                       socket_path=socket)
    socket.chmod(0o600)
    terminal = None
    samples = {}
    for worker in server.workers:
        worker['status'] = {'type': 'idle'}
        server.turns[worker['id']][0].update(status='completed', completedAt=int(time.time()))
    active = server.workers[2]
    rollout = home / 'reused-worker.jsonl'
    active['path'] = str(rollout)
    rollout.write_text('\n'.join(json.dumps(token_usage_record(active['id'], turn, total, lifetime))
        for turn, total, lifetime in [('older-turn', 342061522, 342061522),
                                      ('latest', 679013, 342740535)]) + '\n')
    with sqlite3.connect(home / 'state_5.sqlite') as database:
        database.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, tokens_used INTEGER NOT NULL)')
        database.executemany('INSERT INTO threads VALUES (?, ?)',
            [(worker['id'], 342740535 if worker is active else 1000000)
             for worker in server.workers])
        database_rows = database.execute('SELECT * FROM threads ORDER BY id').fetchall()

    def active_line():
        return next((line for line in worker_rows(terminal.text) if 'worker 3 ·' in line), '')

    def sample(label, expected):
        start = len(server.requests)
        def refreshes():
            return [i for i in range(start, len(server.requests))
                    if server.requests[i]['method'] == 'thread/list'
                    and server.requests[i].get('params', {}).get('parentThreadId') == server.root]
        wait([terminal], lambda: len(refreshes()) >= 4, f'{label}: four refreshes did not finish')
        boundaries = refreshes()
        counts = [Counter(request['params']['threadId']
                          for request in server.requests[left:right]
                          if request['method'] == 'thread/turns/list')
                  for left, right in zip(boundaries, boundaries[1:])]
        samples[label] = counts
        assert all(count == Counter(expected) for count in counts), (label, counts, expected)
        assert_compact_hud(terminal.text)

    try:
        terminal = Terminal(binary, home, workspace)
        wait([terminal], lambda: composer_ready(terminal.text), 'history composer unavailable', 30)
        sample('33 inactive', {})
        assert not worker_rows(terminal.text), terminal.text
        # Stock TUI startup initializes SQLite bookkeeping before HUD-only refreshes.
        database_before = (home / 'state_5.sqlite').read_bytes()
        active['status'] = {'type': 'active', 'activeFlags': []}
        turn = server.turns[active['id']][0]
        turn.update(status='inProgress', startedAt=int(time.time()) - 108, completedAt=None)
        wait([terminal], lambda: '679K tok' in active_line(), 'latest turn used lifetime tokens')
        line = active_line()
        assert '343M tok' not in line, line
        age = re.search(r' · (\d+):(\d{2}) · ', line)
        assert 108 <= int(age[1]) * 60 + int(age[2]) < 130, line
        assert_compact_hud(terminal.text, rows=1)
        (evidence / 'history-current-turn-screen.txt').write_text(terminal.text + '\n')
        turn.update(id='followup-turn', startedAt=int(time.time()))
        wait([terminal], lambda: '? tok' in active_line(), 'new turn inherited prior or lifetime tokens')
        assert re.search(r' · 0:\d{2} · \? tok$', active_line()), active_line()
        with rollout.open('a') as stream:
            stream.write(json.dumps(token_usage_record(active['id'], 'followup-turn', 1234, 342741769)) + '\n')
        rollout_before = rollout.read_bytes()
        wait([terminal], lambda: '1.23K tok' in active_line(), 'follow-up current-turn usage missing')
        assert (home / 'state_5.sqlite').read_bytes() == database_before, 'HUD wrote token database'
        terminal.close()
        terminal = Terminal(binary, home, workspace)
        wait([terminal], lambda: '1.23K tok' in active_line(), 'restart lost reported current-turn usage', 30)
        os.write(terminal.master, b'token-draft')
        wait([terminal], lambda: 'token-draft' in terminal.text, 'token refresh blocked composer')
        assert_compact_hud(terminal.text, rows=1)
        (evidence / 'history-restarted-turn-screen.txt').write_text(terminal.text + '\n')
        # Keep inactive members in an active group, including its oldest creation time.
        for index, member in enumerate(('finished', 'error', 'active')):
            worker = server.workers[index]
            worker['source']['subAgent']['thread_spawn']['agent_path'] = f'/root/history__b1__{member}'
        server.workers[1]['status'] = {'type': 'systemError'}
        server.workers[3]['status'] = {'type': 'notLoaded'}
        wait([terminal], lambda: 'history (1 agent)' in terminal.text and '345M tok' in terminal.text,
             'mixed history group lost inactive members or lifetime tokens')
        sample('one active among history', {active['id']: 1})
        line = next(line for line in worker_rows(terminal.text) if 'history (1 agent)' in line)
        age = re.search(r' · (\d+):(\d{2}) · ', line)
        assert int(age[1]) * 60 + int(age[2]) >= 120, line
        assert line.endswith('345M tok'), line
        for flag, label in [('waitingOnApproval', 'wait approval'), ('waitingOnUserInput', 'wait input')]:
            active['status']['activeFlags'] = [flag]
            wait([terminal], lambda: label in terminal.text, f'{label}: active flag disappeared')
            sample(label, {active['id']: 1})
        active['status'] = {'type': 'idle'}
        wait([terminal], lambda: not worker_rows(terminal.text), 'active-to-idle history did not collapse')
        sample('all inactive after completion', {})
        assert not server.errors, server.errors
        assert not any(request['method'] in ('turn/start', 'turn/steer') for request in server.requests)
        assert (home / 'state_5.sqlite').read_bytes() == database_before, 'HUD wrote token database'
        with sqlite3.connect(f'file:{home / "state_5.sqlite"}?mode=ro', uri=True) as database:
            assert database.execute('SELECT * FROM threads ORDER BY id').fetchall() == database_rows, 'token counters changed'
        assert rollout.read_bytes() == rollout_before, 'HUD wrote worker rollout'
        return ['33 local historical children: zero inactive turn reads; reused worker shows 679K current-turn tokens at 1:48 instead of 343M lifetime; follow-up resets to unknown then 1.23K and survives TUI restart; inactive group members retain 345M lifetime tokens and oldest age; active-to-idle collapse; editable last-six composer; read-only database and zero model calls']
    finally:
        if terminal is not None:
            (evidence / 'history-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        server.close()
        (evidence / 'history-rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')
        (evidence / 'history-request-counts.json').write_text(json.dumps(samples, indent=2) + '\n')
        shutil.copyfile(rollout, evidence / 'history-worker-rollout.jsonl')


def run(binary, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    terminals, servers, stages = [], [], []
    (evidence / 'report.json').unlink(missing_ok=True)
    # Leave room for native Unix sockets under long private TMPDIR paths.
    temporary = tempfile.TemporaryDirectory(prefix='h-')
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
            write_health(home, identifier, 'uncertain', uncertain=987654)
            (home / 'codex-plus' / f'{identifier}.advisor.json').write_text(json.dumps(dict(
                version=1, parent=identifier, model='gpt-99-astra', effort='xhigh', state='running',
                started_at=time.time(), updated_at=time.time(), finished_at=None, calls=987654,
                last_duration_secs=None, error=None)))
            # A remote worker must never display an unrelated local cache's tokens.
            with sqlite3.connect(home / 'state_5.sqlite') as database:
                database.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, tokens_used INTEGER NOT NULL)')
                database.executemany('INSERT INTO threads VALUES (?, ?)', [(worker['id'], 987654) for worker in server.workers])
            for worker in server.workers:
                path = home / f"{worker['id']}.jsonl"
                path.write_text(json.dumps(token_usage_record(worker['id'], 'latest', 987654, 987654)) + '\n')
                worker['path'] = str(path)
            servers.append(server)
            terminals.append(Terminal(binary, home, workspace, server.url))

        wait(terminals, lambda: len(worker_rows(terminals[0].text)) == 2 and '+4 more' in terminals[1].text,
             'distinct worker populations did not render', 30)
        for server, terminal in zip(servers, terminals):
            assert 'for shortcuts' not in terminal.text and 'for agents' not in terminal.text, terminal.text
            assert_compact_hud(terminal.text, rows=min(3, len(server.workers)))
            worker_lines = worker_rows(terminal.text)
            assert len(worker_lines) == 2, terminal.text
            for index, line in enumerate(worker_lines):
                assert ('GPT-6.1-Sol (high)' if index % 2 == 0 else 'GPT-6-Astra (medium)') + f' [general] · worker {index + 1} ·' in line, line
                assert '? tok' in line, line
                assert re.search(r' · \d+:\d{2} · ', line), line
                assert '/root/' not in line and 'worker_' not in line, line
            if len(server.workers) > 3:
                assert '+4 more' in terminal.text and 'worker 3 ·' not in terminal.text, terminal.text
            assert 'idle ' not in terminal.text and 'done ' not in terminal.text and 'other ' not in terminal.text, terminal.text
            assert 'Wake delivery uncertain' not in terminal.text and '987654' not in terminal.text, terminal.text
            assert not advisor_rows(terminal.text), terminal.text
        (evidence / 'compact-six-active-screen.txt').write_text(terminals[1].text + '\n')
        stages.append('six active workers show two full-model rows plus +4 more in three total rows, with elapsed, unknown remote tokens, actual GPT-6.1-Sol high/GPT-6-Astra medium, and idle composer within last six nonempty lines')
        os.write(terminals[0].master, b'/daemon')
        wait(terminals, lambda: '/daemon' in terminals[0].text, '/daemon did not finish rendering')
        os.write(terminals[0].master, b'\r')
        wait(terminals, lambda: 'Daemon updates are disabled in Codex Plus' in terminals[0].text,
             '/daemon did not show read-only status')
        assert 'Close' in terminals[0].text and 'Manage this server on its host.' in terminals[0].text
        assert all(choice not in terminals[0].text for choice in
                   ('Install latest public stable', 'Use this CLI build', 'Update and exit')), terminals[0].text
        (evidence / 'daemon-status-screen.txt').write_text(terminals[0].text + '\n')
        os.write(terminals[0].master, b'\r')
        wait(terminals, lambda: 'Daemon updates are disabled' not in terminals[0].text
             and len(worker_rows(terminals[0].text)) == 2, 'closing /daemon did not return to the composer')
        stages.append('read-only /daemon shows status guidance and Close; no daemon update choices or exit')
        for index, terminal in enumerate(terminals):
            assert ('+4 more' in terminal.text) == (index == 1), terminal.text
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
        wait(terminals, lambda: '+3 more' in terminals[1].text and 'worker 1 ·' not in terminals[1].text and 'worker 3 ·' in terminals[1].text, 'completed worker did not disappear and promote hidden active worker')
        assert_compact_hud(terminals[1].text, rows=3)
        (evidence / 'compact-promoted-screen.txt').write_text(terminals[1].text + '\n')
        stages.append('completion removes its row and promotes a hidden active worker')
        for worker in servers[1].workers[1:-1]:
            worker['status'] = {'type': 'idle'}
            servers[1].turns[worker['id']][0].update(status='completed', completedAt=int(time.time()))
        wait(terminals, lambda: len(worker_rows(terminals[1].text)) == 1 and 'worker 6 ·' in terminals[1].text and '+3 more' not in terminals[1].text, 'remaining active worker did not replace completed rows')
        last_worker = servers[1].workers[-1]
        last_worker['status'] = {'type': 'idle'}
        servers[1].turns[last_worker['id']][0].update(status='completed', completedAt=int(time.time()))
        wait(terminals, lambda: not worker_rows(terminals[1].text) and 'worker 6 ·' not in terminals[1].text and '+3 more' not in terminals[1].text, 'healthy HUD did not disappear after the last worker finished')
        os.write(terminals[1].master, b'-quiet')
        wait(terminals, lambda: 'draft-1-quiet' in terminals[1].text, 'hidden HUD blocked the composer')
        assert len(worker_rows(terminals[0].text)) == 2 and 'draft-0-refresh' in terminals[0].text, terminals[0].text
        assert_compact_hud(terminals[1].text, rows=0)
        (evidence / 'compact-all-finished-screen.txt').write_text(terminals[1].text + '\n')
        stages.append('last completion hides the entire strip while both composers remain editable')
        for worker in servers[1].workers:
            worker['status'] = {'type': 'active', 'activeFlags': []}
            servers[1].turns[worker['id']][0].update(status='inProgress', startedAt=int(time.time()), completedAt=None)
        wait(terminals, lambda: '+4 more' in terminals[1].text and 'worker 1 ·' in terminals[1].text and 'draft-1-quiet' in terminals[1].text, 'new runtime activity did not restore compact HUD')
        stages.append('later activity restores the compact strip without losing the draft')

        grouped_server, grouped_terminal = servers[1], terminals[1]
        for index, member in enumerate(('render', 'test', 'review')):
            worker = grouped_server.workers[index]
            worker['source']['subAgent']['thread_spawn']['agent_path'] = f'/root/hud_styling__b1__{member}'

        def group_line():
            return next((line.strip() for line in grouped_terminal.screen.display if 'hud styling (' in line), '')

        wait(terminals, lambda: '(3 agents)' in group_line() and '+2 more' in grouped_terminal.text, 'explicit cohort did not replace three individual rows')
        assert group_line().startswith('mixed (mixed) [general] ·') and '? tok' in group_line(), group_line()
        assert_compact_hud(grouped_terminal.text, rows=3)
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
            wait(terminals, lambda: (f'({remaining} agent{suffix})' in group_line() if remaining else not group_line()), f'group completion failed to show {remaining} remaining members')
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
        wait(terminals, lambda: not worker_rows(grouped_terminal.text) and '+2 more' not in grouped_terminal.text and 'draft-1-quiet-group' in grouped_terminal.text, 'group and independent cohort did not collapse after completion')
        stages.append('explicit three-member cohort reduces 3→2→1→hidden without duplicate rows or shrinking dispatch elapsed; independent cohort stays separate, group resize/input preserve draft, final strip collapses')
        for worker in grouped_server.workers:
            worker['status'] = {'type': 'active', 'activeFlags': []}
            grouped_server.turns[worker['id']][0].update(status='inProgress', startedAt=int(time.time()), completedAt=None)
        wait(terminals, lambda: '(3 agents)' in group_line() and '+2 more' in grouped_terminal.text, 'group reactivation did not restore strip')
        grouped_server.workers[-1]['status']['activeFlags'] = ['waitingOnApproval']
        wait(terminals, lambda: any('worker 6 · wait approval' in line for line in worker_rows(grouped_terminal.text)),
             'explicit approval wait did not remain visible')
        grouped_server.reject_turn_metadata = True
        wait(terminals, lambda: 'Workers stale' in grouped_terminal.text and '+3 more' in grouped_terminal.text,
             'worker alert and overflow did not share the three-row budget')
        assert_compact_hud(grouped_terminal.text, rows=3)
        assert len(worker_rows(grouped_terminal.text)) == 1 and 'worker 6 · stale' in grouped_terminal.text, grouped_terminal.text
        (evidence / 'worker-wait-alert-overflow-screen.txt').write_text(grouped_terminal.text + '\n')
        grouped_server.reject_turn_metadata = False
        grouped_server.workers[-1]['status']['activeFlags'] = []
        wait(terminals, lambda: 'Workers stale' not in grouped_terminal.text and '(3 agents)' in group_line() and '+2 more' in grouped_terminal.text,
             'worker wait and stale state failed to recover')
        stages.append('explicit approval wait survives alert plus overflow in three total rows, with idle composer within last six nonempty lines')

        replies_before_resize = terminals[0].resize(10, 50)
        wait(terminals, lambda: terminals[0].resized_frame(replies_before_resize) and 'draft-0-refresh' in terminals[0].text, 'small terminal did not finish repainting')
        os.write(terminals[0].master, b'-small')
        wait(terminals, lambda: 'draft-0-refresh-small' in terminals[0].text, 'small terminal lost composer')
        assert worker_rows(terminals[0].text), terminals[0].text
        assert_compact_hud(terminals[0].text, rows=2)
        (evidence / 'small-screen.txt').write_text(terminals[0].text + '\n')
        replies_before_resize = terminals[0].resize(32, 120)
        wait(terminals, lambda: terminals[0].resized_frame(replies_before_resize) and len(worker_rows(terminals[0].text)) == 2 and 'draft-0-refresh-small' in terminals[0].text, 'normal resize did not restore HUD')
        stages.append('small and normal PTY resize with preserved draft')

        servers[0].reject_turn_metadata = True
        wait(terminals, lambda: 'Workers stale' in terminals[0].text and 'counts unavailable' in terminals[0].text, 'partial metadata failure did not mark snapshot stale')
        assert 'draft-0-refresh-small' in terminals[0].text, terminals[0].text
        assert_compact_hud(terminals[0].text, rows=3)
        (evidence / 'metadata-error-screen.txt').write_text(terminals[0].text + '\n')
        servers[0].reject_turn_metadata = False
        wait(terminals, lambda: len(worker_rows(terminals[0].text)) == 2 and 'Workers stale' not in terminals[0].text, 'metadata recovery did not restore counts')
        stages.append('partial metadata RPC failure marks counts stale and recovers')

        servers[0].disconnect()
        wait(terminals, lambda: 'Workers stale' in terminals[0].text and 'counts unavailable' in terminals[0].text, 'disconnect did not mark counts stale')
        os.write(terminals[0].master, b'-offline')
        wait(terminals, lambda: 'draft-0-refresh-small-offline' in terminals[0].text, 'disconnect blocked draft editing')
        assert_compact_hud(terminals[0].text, rows=3)
        assert '(3 agents)' in group_line() and '+2 more' in terminals[1].text and 'draft-1-quiet' in terminals[1].text, terminals[1].text
        (evidence / 'stale-screen.txt').write_text(terminals[0].text + '\n')
        servers[0].available.set()
        wait(terminals, lambda: len(worker_rows(terminals[0].text)) == 2 and 'Workers stale' not in terminals[0].text and 'draft-0-refresh-small-offline' in terminals[0].text, 'reconnect failed to recover HUD and draft', 30)
        stages.append('stale disconnect and automatic reconnect without cross-pane effect')

        for server in servers:
            assert not server.errors, server.errors
            assert not any(request.get('method') in ('turn/start', 'turn/steer') for request in server.requests), server.requests
        stages.append('zero turn/start or turn/steer requests and zero model calls')
        stages.append('remote websocket roots ignore unrelated local watcher and advisor status files')
        stages.extend(worker_history_smoke(binary, root, evidence))
        stages.extend(tmux_smoke(binary, root, evidence))
        stages.extend(local_health_smoke(binary, root, evidence))
        stages.extend(shell_hud_smoke(binary, root, evidence))
        stages.extend(local_shell_smoke(binary, root, evidence))
        stages.extend(advisor_hud_smoke(binary, root, evidence))
        stages.extend(advisor_transcript_smoke(binary, root, evidence))
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
    report = {'binary': str(binary), 'passed': stages, 'ui_turn_start_requests': 0, 'ui_turn_steer_requests': 0, 'cursor_replies': [terminal.cursor_replies for terminal in terminals], 'model_calls_after_fixture_setup': 0, 'fixture_seed_model_calls': 2, 'fixture_shell_model_calls': 2, 'fixture_advisor_seed_model_calls': 2, 'fixture_advisor_shell_model_calls': 4, 'fixture_advisor_transcript_model_calls': 6, 'terminal_parser': 'pyte 0.8.2', 'tmux_required': False}
    (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, default=Path('/tmp/codex-plus-native-smoke-evidence'))
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve())
