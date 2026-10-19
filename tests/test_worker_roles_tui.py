"""Worker roles survive native HUD aggregation and clipping; no model calls."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time

from fixtures.native_server import AppServer
from native_smoke import Terminal, assert_compact_hud, composer_ready, wait, worker_rows


def run(binary, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    terminal = None
    stages = []
    with tempfile.TemporaryDirectory(prefix='plus-roles-') as temporary:
        root = Path(temporary)
        home, work = root / 'home', root / 'work'
        home.mkdir(mode=0o700)
        work.mkdir()
        (home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="openai"\n'
            'suppress_unstable_features_warning=true\nanalytics.enabled=false\n'
            'check_for_update_on_startup=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n')
        (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
        socket = home / 'app-server-control/app-server-control.sock'
        socket.parent.mkdir(mode=0o700)
        server = AppServer('00000000-0000-0000-0010-000000000001', work, 3, socket_path=socket)
        socket.chmod(0o600)
        for worker, role in zip(server.workers, ['worker', 'explorer', 'tester']):
            worker.update(agentRole=role, model='gpt-6.1-sol', reasoningEffort='high')

        def rows_match(labels):
            rows = worker_rows(terminal.text)
            return len(rows) == len(labels) and all(
                row.startswith(f'GPT-6.1-Sol (high) [{role}] · {task} · ')
                for row, (role, task) in zip(rows, labels))

        def check(stage, labels):
            wait([terminal], lambda: rows_match(labels), f'worker role stage {stage} failed')
            assert_compact_hud(terminal.text, rows=len(labels))
            assert not server.errors, server.errors
            assert not [r for r in server.requests if r['method'] in ('turn/start', 'turn/steer')]
            (evidence / (stage + '.txt')).write_text(terminal.text + '\n')
            stages.append(stage)

        try:
            terminal = Terminal(binary, home, work)
            wait([terminal], lambda: composer_ready(terminal.text) and len(worker_rows(terminal.text)) == 3,
                 'native worker rows did not appear', 25)
            expected = [('worker', 'worker 1'), ('explorer', 'worker 2'), ('tester', 'worker 3')]
            assert rows_match(expected), 'worker roles missing from native HUD rows\n' + terminal.text
            check('standalone-builtins', expected)

            for worker, role in zip(server.workers, ['release_reviewer', None, '']):
                worker['agentRole'] = role
            del server.workers[1]['agentRole']
            check('custom-and-unknown', [('release_reviewer', 'worker 1'), ('general', 'worker 2'), ('general', 'worker 3')])
            for worker, role in zip(server.workers, ['\ncustom\trole\x07', '\n\t\x07', '   ']):
                worker['agentRole'] = role
            check('controls-and-empty', [('custom role', 'worker 1'), ('general', 'worker 2'), ('general', 'worker 3')])

            for index, worker in enumerate(server.workers):
                worker['source']['subAgent']['thread_spawn']['agent_path'] = f'/root/role_checks__b1__member{index}'
                worker['agentRole'] = '\nworker\t' if index == 0 else 'worker'
            check('group-normalized-same', [('worker', 'role checks (3 agents)')])
            server.workers[1]['agentRole'] = 'explorer'
            check('group-mixed', [('mixed', 'role checks (3 agents)')])
            server.workers[2]['agentRole'] = None
            check('group-unknown', [('general', 'role checks (3 agents)')])
            for worker in server.workers:
                worker['agentRole'] = 'worker'
            check('group-recovered', [('worker', 'role checks (3 agents)')])
            server.workers[2]['agentRole'] = '\n\t\x07'
            check('group-controls-unknown', [('general', 'role checks (3 agents)')])

            server.workers[0]['agentRole'] = server.workers[1]['agentRole'] = 'worker'
            finished = server.workers[2]
            finished['agentRole'] = 'tester'
            finished['status'] = {'type': 'idle'}
            server.turns[finished['id']][0].update(status='completed', completedAt=int(time.time()))
            check('group-finished-role-excluded', [('worker', 'role checks (2 agents)')])
            server.workers[1]['status'] = {'type': 'active', 'activeFlags': ['waitingOnApproval']}
            check('group-role-before-wait', [('worker', 'role checks (2 agents) · wait approval')])
            server.workers[1]['status'] = {'type': 'active', 'activeFlags': []}
            wait([terminal], lambda: 'wait approval' not in terminal.text, 'group wait state remained')
            check('group-wait-cleared', [('worker', 'role checks (2 agents)')])

            os.write(terminal.master, b'role-draft')
            wait([terminal], lambda: 'role-draft' in terminal.text, 'worker role blocked composer')
            replies = terminal.resize(12, 65)
            wait([terminal], lambda: terminal.resized_frame(replies) and 'role-draft' in terminal.text
                 and len(worker_rows(terminal.text)) == 1
                 and '[worker] · ' in worker_rows(terminal.text)[0]
                 and '(2 agents) · ' in worker_rows(terminal.text)[0],
                 'narrow group lost role or draft')
            assert_compact_hud(terminal.text, rows=1)
            (evidence / 'narrow-group-editable.txt').write_text(terminal.text + '\n')
            stages.append('narrow-group-editable')
            server.workers[0]['source']['subAgent']['thread_spawn']['agent_path'] = '/root/' + 'long_task_' * 12
            server.workers[1]['status'] = {'type': 'idle'}
            server.turns[server.workers[1]['id']][0].update(status='completed', completedAt=int(time.time()))
            wait([terminal], lambda: len(worker_rows(terminal.text)) == 1 and
                 ' [worker] · ' in worker_rows(terminal.text)[0] and '…' in worker_rows(terminal.text)[0],
                 'task clipping lost worker role')
            assert_compact_hud(terminal.text, rows=1)
            assert 'role-draft' in terminal.text, terminal.text
            (evidence / 'narrow-long-task.txt').write_text(terminal.text + '\n')
            stages.append('narrow-long-task')
            for worker in server.workers:
                worker['status'] = {'type': 'idle'}
                server.turns[worker['id']][0].update(status='completed', completedAt=int(time.time()))
            wait([terminal], lambda: not worker_rows(terminal.text) and 'role-draft' in terminal.text,
                 'completed workers kept role rows or lost draft')
            assert_compact_hud(terminal.text, rows=0)
            assert not server.errors, server.errors
            assert not [r for r in server.requests if r['method'] in ('turn/start', 'turn/steer')]
            stages.append('completed')
            report = dict(passed=True, binary=str(binary), display_model_requests=0, stages=stages)
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            return report
        finally:
            if terminal:
                (evidence / 'last-screen.txt').write_text(terminal.text + '\n')
                (evidence / 'last.ansi').write_bytes(terminal.output)
                terminal.close()
            server.close()
            (evidence / 'rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.binary.resolve(), args.evidence.resolve()), indent=2))
