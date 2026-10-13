"""Isolated real native server acceptance; never opens the production CODEX_HOME.

Run: python3 tests/test_background_integration.py .build/codex
"""

import argparse
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

from fixtures.background_provider import ResponsesProvider


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from codex_hud.rpc import Client


class RecordingClient(Client):
    def __init__(self, path):
        self.messages = []
        super().__init__(path, timeout=15)

    def receive(self):
        message = super().receive()
        self.messages.append(message)
        return message


def wait(check, reason, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError(reason)


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def coordinator_state(home):
    """Read only the fixture's owned coordinator database, never native state."""
    with sqlite3.connect(f'file:{home / "codex-hud" / "background.sqlite"}?mode=ro', uri=True) as database:
        database.row_factory = sqlite3.Row
        return {table: [dict(row) for row in database.execute(f'SELECT * FROM {table}')]
                for table in ('parks', 'jobs', 'callbacks')}


def stop_observers(home):
    # Stop fixture supervisors first so they cannot respawn observers during cleanup.
    for action in (b'supervise', b'observe'):
        for path in Path('/proc').glob('[0-9]*'):
            try:
                command = (path / 'cmdline').read_bytes().split(b'\0')
                environment = (path / 'environ').read_bytes().split(b'\0')
                if b'codex_hud.background' in command and action in command and f'CODEX_HOME={home}'.encode() in environment:
                    descriptor = os.pidfd_open(int(path.name))
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                        if not select.select([descriptor], [], [], 3)[0]:
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                            assert select.select([descriptor], [], [], 3)[0], 'fixture observer did not exit'
                    finally:
                        os.close(descriptor)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue


def run(binary, evidence, probe_only=False):
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'binary': str(binary), 'remote_model_requests': 0}
    provider = rpc = attached = native = None
    with tempfile.TemporaryDirectory(prefix='codex-hud-background-') as temporary:
        root = Path(temporary)
        home, workspace = root / 'home', root / 'workspace'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        release = workspace / 'release'
        command = (f'deadline=$((SECONDS+30)); while [ ! -e {shlex.quote(str(release))} ] && '
                   f'(( SECONDS < deadline )); do sleep 0.1; done; '
                   f'test -e {shlex.quote(str(release))} || exit 124; echo canonical-completion')
        provider = ResponsesProvider(command)
        env = {key: value for key, value in os.environ.items()
               if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        env.update(CODEX_HOME=str(home), PYTHONPATH=str(REPO), RUST_LOG='warn')
        (home / 'config.toml').write_text(f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
analytics.enabled=false
features.daemon_auto_start=false
features.unified_exec=true
features.shell_tool=true
features.plugins=false
[model_providers.isolated]
name="loopback acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
        native_log = (evidence / 'native.stderr').open('w')
        try:
            native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
                                      stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log,
                                      start_new_session=True)
            socket_path = home / 'app-server-control' / 'app-server-control.sock'
            wait(lambda: socket_path.exists(), 'isolated Unix app-server did not start')
            rpc = RecordingClient(socket_path)
            parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
            turn = rpc.call('turn/start', {'threadId': parent,
                'input': [{'type': 'text', 'text': 'Start finite held command then park.', 'text_elements': []}],
                'effort': 'high'})['turn']['id']
            wait(lambda: provider.count == 2, 'command did not yield to final provider response')
            active_thread = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
            active_terminals = rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']
            assert active_thread['status']['type'] == 'active' and len(active_terminals) == 1
            process = active_terminals[0]['processId']
            report.update(active_thread=active_thread, active_terminals=active_terminals)
            if not probe_only:
                parked = subprocess.run([sys.executable, '-m', 'codex_hud.background', 'park',
                    '--parent', parent, '--process', process, '--reason', 'isolated acceptance'],
                    cwd=REPO, env=env, capture_output=True, text=True, timeout=15)
                (evidence / 'park.stdout').write_text(parked.stdout)
                (evidence / 'park.stderr').write_text(parked.stderr)
                assert parked.returncode == 0, parked.stderr
                report['registered_state'] = coordinator_state(home)
                assert report['registered_state']['jobs'][0]['turn'] is None, 'fixture should exercise unknown-origin registration'
            provider.final_gate.set()
            wait(lambda: rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']['turns'][-1]['status']
                 != 'inProgress', 'parent first turn did not complete')
            thread = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
            terminals = rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']
            item_page = rpc.call('thread/items/list', {'threadId': parent, 'turnId': turn})
            turn_page = rpc.call('thread/turns/list', {'threadId': parent, 'itemsView': 'full'})
            report.update(parent=parent, first_turn=turn, idle_thread=thread, running_terminals=terminals,
                          idle_item_page=item_page, idle_turn_page=turn_page)
            assert thread['status']['type'] == 'idle', thread
            assert thread['turns'][-1]['status'] == 'completed', thread
            assert len(terminals) == 1, terminals
            process = terminals[0]['processId']
            report['process'] = process
            before = provider.count
            time.sleep(1)
            assert provider.count == before == 2, provider.requests
            assert rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data'], 'command exited during idle interval'
            report['idle_observation'] = {'seconds': 1, 'provider_requests_before': before,
                                          'provider_requests_after': provider.count}
            if probe_only:
                attached = RecordingClient(socket_path)
                report['minimal_attach'] = attached.call('thread/resume', {'threadId': parent, 'excludeTurns': True})
                after_attach = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
                for key in ('id', 'model', 'modelProvider', 'reasoningEffort', 'cwd', 'status', 'turns'):
                    assert after_attach[key] == thread[key], f'minimal attach changed {key}'
                assert provider.count == 2, 'minimal attach called model'
            if not probe_only:
                time.sleep(1)
                assert provider.count == 2, 'model called while parent was parked'
                assert rpc.call('thread/read', {'threadId': parent})['thread']['status']['type'] == 'idle'
                assert rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']
                report['parked_idle_observation'] = {'seconds': 1, 'loopback_provider_requests': provider.count}
            release.touch()
            wait(lambda: not rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data'],
                 'finite command did not leave background terminal registry')
            report['after_completion_items'] = rpc.call('thread/items/list', {'threadId': parent, 'turnId': turn})
            report['after_completion_thread'] = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
            if attached:
                attached.call('thread/read', {'threadId': parent})
            completed = [message for message in (attached or rpc).messages if message.get('method') == 'item/completed'
                and message['params']['item'].get('type') == 'commandExecution'
                and message['params']['item'].get('processId') == process]
            assert len(completed) == 1, (attached or rpc).messages
            assert completed[0]['params']['turnId'] == turn, completed
            report['canonical_command_completion'] = completed[0]
            if not probe_only:
                wait(lambda: provider.count >= 3, 'completion did not resume parent once')
                wait(lambda: len(rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']['turns']) == 2,
                     'resume turn was not recorded')
                time.sleep(.5)
                thread = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
                assert provider.count == 3, provider.requests
                assert thread['status']['type'] == 'idle' and len(thread['turns']) == 2, thread
                report['after_resume_thread'] = thread
                status = subprocess.run([sys.executable, '-m', 'codex_hud.background', 'park',
                    '--parent', parent, '--status'], cwd=REPO, env=env,
                    capture_output=True, text=True, timeout=10)
                assert status.returncode == 0, status.stderr
                wait(lambda: (state := coordinator_state(home))['callbacks'] and
                     all(callback['state'] == 'delivered' for callback in state['callbacks']),
                     'owned callback was not reconciled as delivered', timeout=5)
                report['coordinator_state'] = coordinator_state(home)
                callbacks = report['coordinator_state']['callbacks']
                assert len(callbacks) == 1 and callbacks[0]['state'] == 'delivered', callbacks
                consumed = [item for recorded_turn in thread['turns'] for item in recorded_turn['items']
                            if item.get('type') == 'userMessage' and item.get('clientId') == callbacks[0]['client']]
                assert len(consumed) == 1, consumed
                report['queue_after_resume'] = rpc.call('thread/queue/list', {'threadId': parent})
                assert not report['queue_after_resume']['data'], 'owned wake remained queued after resume'
                jobs = report['coordinator_state']['jobs']
                assert len(jobs) == 1 and jobs[0]['turn'] == turn and jobs[0]['status'] == 'completed', jobs
                restarted = subprocess.run([sys.executable, '-m', 'codex_hud.background', 'observe', '--parent', parent],
                    cwd=REPO, env=env, capture_output=True, text=True, timeout=10)
                report['observer_restart'] = {'returncode': restarted.returncode, 'stdout': restarted.stdout,
                                               'stderr': restarted.stderr}
                assert restarted.returncode == 0, restarted.stderr
                time.sleep(.5)
                assert provider.count == 3, 'observer restart submitted duplicate wake'
                assert len(rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']['turns']) == 2
            report['passed'] = True
        finally:
            provider.final_gate.set()
            release.touch()
            if attached:
                attached.close()
            if rpc:
                rpc.close()
            if native:
                stop(native)
            stop_observers(home)
            native_log.close()
            for path in home.glob('sessions/**/*.jsonl'):
                shutil.copy2(path, evidence / path.name)
            for path in (home / 'codex-hud').glob('*'):
                if path.is_file() and path.suffix in ('.json', '.log'):
                    shutil.copy2(path, evidence / path.name)
            report['loopback_provider_requests'] = provider.count
            (evidence / 'provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            (evidence / 'rpc-notifications.json').write_text(json.dumps(rpc.messages if rpc else [], indent=2) + '\n')
            (evidence / 'attach-notifications.json').write_text(json.dumps(attached.messages if attached else [], indent=2) + '\n')
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            provider.close()
    print(json.dumps({key: report[key] for key in ('passed', 'parent', 'process',
          'loopback_provider_requests', 'remote_model_requests')}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, default=REPO / '.build' / 'auto-resume-acceptance')
    parser.add_argument('--probe-only', action='store_true')
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve(), args.probe_only)
