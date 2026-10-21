"""Detached status projection in the actual TUI; local/remote loopback fixtures only."""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from fixtures.native_server import AppServer
from native_smoke import Terminal, advisor_rows, assert_compact_hud, composer_ready, wait


def run(binary, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'binary': str(binary), 'fixture': 'loopback projection, no workload launch'}
    terminals, servers = [], []
    with tempfile.TemporaryDirectory(prefix='plus-dhud-') as temporary:
        base = Path(temporary)
        home, work = base / 'home', base / 'work'
        home.mkdir(mode=0o700)
        work.mkdir()
        (home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="openai"\n'
            'suppress_unstable_features_warning=true\nanalytics.enabled=false\n'
            'features.daemon_auto_start=false\nfeatures.plugins=false\n')
        (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
        socket = home / 'app-server-control/app-server-control.sock'
        socket.parent.mkdir(mode=0o700)
        root = '00000000-0000-0000-0009-000000000001'
        server = AppServer(root, work, 0, socket_path=socket)
        servers.append(server)
        socket.chmod(0o600)
        directory = home / 'codex-plus/execution-status' / root
        directory.mkdir(parents=True)
        identity = str(uuid.uuid4())
        path = directory / f'{identity}.json'
        started = time.time() - 125

        def publish(state='running', stage=None, age=0, error=None):
            value = dict(schema=1, id=identity, root=root, topic='compile 模型', state=state,
                         started_at=started, updated_at=time.time() - age, stage=stage, error=error)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(value))
            os.replace(temporary, path)

        def capture(label):
            assert 'Background status unavailable' not in terminals[0].text
            assert advisor_rows(terminals[0].text) == ['Advisor · ready · 0 calls'], terminals[0].text
            (evidence / f'{label}.txt').write_text(terminals[0].text + '\n')

        try:
            publish(stage='link')
            terminal = Terminal(binary, home, work)
            terminals.append(terminal)
            wait(terminals, lambda: composer_ready(terminal.text) and
                 'Background · compile 模型 · link · 2:' in terminal.text and
                 'Workers stale' not in terminal.text and 'Shells stale' not in terminal.text,
                 'detached execution absent or durable elapsed lost', 30)
            assert_compact_hud(terminal.text, rows=2)
            assert 'seen' not in terminal.text
            capture('active')
            os.write(terminal.master, b'detached-draft')
            wait(terminals, lambda: 'detached-draft' in terminal.text, 'draft blocked')
            publish()
            wait(terminals, lambda: 'Background · compile 模型 · 2:' in terminal.text and
                 ' · link · ' not in terminal.text, 'optional stage was inferred or retained')
            publish(state='unknown')
            wait(terminals, lambda: 'compile 模型 · unknown' in terminal.text, 'unknown outcome hidden')
            assert_compact_hud(terminal.text, rows=2)
            capture('unknown')
            publish(age=20)
            wait(terminals, lambda: 'compile 模型 · stale' in terminal.text, 'stale heartbeat hidden')
            replies = terminal.resize(12, 65)
            wait(terminals, lambda: terminal.resized_frame(replies) and
                 'detached-draft' in terminal.text and 'compile 模型 · stale' in terminal.text,
                 'narrow status lost input or warning')
            assert_compact_hud(terminal.text, rows=2)
            capture('stale-narrow')
            replies = terminal.resize(32, 120)
            publish(error='monitoring unavailable')
            wait(terminals, lambda: terminal.resized_frame(replies)
                 and 'Background · compile 模型 · 2:' in terminal.text
                 and 'compile 模型 · stale' not in terminal.text, 'projection error hid the job or retained stale state')
            assert json.loads(path.read_text())['error'] == 'monitoring unavailable'
            # Unrelated native jobs remain native rows; all activity shares one cap.
            server.shells = [server.shell(42, 'unrelated-native-job')]
            wait(terminals, lambda: 'unrelated-native-job' in terminal.text, 'native job hidden by detached identity')
            assert_compact_hud(terminal.text, rows=3)
            capture('mixed')
            published = path.read_text()
            path.write_text('{')
            wait(terminals, lambda: 'compile 模型' not in terminal.text, 'malformed record was accepted')
            assert path.read_text() == '{', 'display rewrote invalid execution evidence'
            assert not any(word in terminal.text.lower() for word in ('completed', 'succeeded', 'exit 0'))
            capture('invalid-record')
            path.write_text(published)
            wait(terminals, lambda: 'compile 模型' in terminal.text, 'valid record did not return after invalid snapshot')
            path.unlink()
            wait(terminals, lambda: 'compile 模型' not in terminal.text and 'Background status unavailable' not in terminal.text,
                 'removed projection did not disappear')
            assert 'unrelated-native-job' in terminal.text
            assert not any(word in terminal.text.lower() for word in ('completed', 'succeeded', 'exit 0'))
            capture('record-removed')
            server.shells = []
            publish()
            wait(terminals, lambda: 'compile 模型' in terminal.text and 'unrelated-native-job' not in terminal.text,
                 'restart precondition did not settle')
            first_elapsed = re.search(r'compile 模型 · (\d+):(\d{2})', terminal.text)
            assert first_elapsed, terminal.text
            terminal.close()
            terminals.clear()
            terminal = Terminal(binary, home, work)
            terminals.append(terminal)
            wait(terminals, lambda: 'Background · compile 模型 · 2:' in terminal.text,
                 'TUI restart reset durable elapsed', 30)
            capture('restart')
            # Another root's local records never appear; remote sockets ignore even exact-root decoys.
            other = home / 'codex-plus/execution-status' / str(uuid.uuid4())
            other.mkdir()
            (other / f'{uuid.uuid4()}.json').write_text('{"topic":"foreign-root-decoy"}')
            remote = AppServer(root, work, 0)
            servers.append(remote)
            remote_terminal = Terminal(binary, home, work, endpoint=remote.url)
            terminals.append(remote_terminal)
            wait(terminals, lambda: composer_ready(remote_terminal.text), 'remote decoy terminal failed', 30)
            # Wait for positive refreshes, rather than interpreting initial absence as isolation.
            wait(terminals, lambda: sum(request['method'] == 'thread/backgroundTerminals/list'
                 for request in remote.requests) >= 3, 'remote HUD refresh did not run')
            assert 'compile 模型' not in remote_terminal.text and 'foreign-root-decoy' not in terminal.text
            (evidence / 'remote-decoy.txt').write_text(remote_terminal.text + '\n')
            for current in servers:
                assert not current.errors, current.errors
                assert not [request for request in current.requests if request['method'] in ('turn/start', 'turn/steer')]
            report.update(passed=True, display_model_requests=0, stages=['active', 'unknown', 'stale-narrow',
                          'projection-error', 'mixed', 'invalid-record', 'record-removed', 'restart', 'root-and-remote-isolation'])
        finally:
            for index, terminal in enumerate(terminals):
                (evidence / f'pane-{index}.ansi').write_bytes(terminal.output)
                terminal.close()
            for index, current in enumerate(servers):
                (evidence / f'rpc-{index}.json').write_text(json.dumps(current.requests, indent=2) + '\n')
                current.close()
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve())
