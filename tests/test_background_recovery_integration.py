"""Real daemon restart/crash checks in a private CODEX_HOME and loopback provider.

Run: python3 tests/test_background_recovery_integration.py .build/codex
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from test_background_integration import (
    REPO, RecordingClient, coordinator_state, stop,
    stop_observers, wait,
)
from codex_plus.background import Store
from fixtures.recovery_provider import RecoveryProvider, WorkerProvider

SCENARIOS = ('shell-restart', 'worker-restart', 'before-enqueue', 'after-enqueue', 'after-start', 'queue-restart', 'native-replay')


def run(binary, evidence, scenario='shell-restart'):
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'binary': str(binary), 'scenario': scenario, 'remote_model_requests': 0}
    provider = rpc = native = observer = None
    with tempfile.TemporaryDirectory(prefix='codex-plus-recovery-') as temporary:
        root = Path(temporary)
        home, workspace = root / 'home', root / 'workspace'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        release = workspace / 'release'
        runs = workspace / 'runs'
        command = (f'echo run >> {shlex.quote(str(runs))}; '
                   f'deadline=$((SECONDS+60)); while [ ! -e {shlex.quote(str(release))} ] && '
                   f'(( SECONDS < deadline )); do sleep 0.1; done; '
                   f'test -e {shlex.quote(str(release))} || exit 124; echo recovery-completion')
        provider = WorkerProvider() if scenario == 'worker-restart' else RecoveryProvider(command)
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
features.multi_agent=true
[model_providers.isolated]
name="loopback recovery acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
        socket_path = home / 'app-server-control' / 'app-server-control.sock'
        native_log = (evidence / 'native.stderr').open('w')
        observer_log = (evidence / 'observer.stderr').open('w')

        def start_native():
            process = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
                                       stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log,
                                       start_new_session=True)

            def connect():
                if process.poll() is not None:
                    raise AssertionError(f'isolated native server exited {process.returncode}')
                try:
                    return RecordingClient(socket_path)
                except (OSError, ValueError):
                    return None

            try:
                return process, wait(connect, 'isolated native server did not accept connections')
            except Exception:
                stop(process)
                raise

        def start_observer(fault=None):
            source = '''import os, time
from codex_plus.background import Store, Observer
s = Store(os.environ["CODEX_HOME"])
def fault(point):
    if point == 'before_enqueue' and os.environ.get('RECOVERY_QUEUE_BLOCK') == '1':
        o.call('turn/start', {'threadId': o.parent,
            'input': [{'type': 'text', 'text': 'Hold fixture response.', 'text_elements': []}]})
    if point == os.environ.get('RECOVERY_FAULT'):
        if os.environ.get('RECOVERY_AFTER_CONSUMED') == '1':
            deadline = time.monotonic() + 10
            client = s.db.execute('SELECT client FROM callbacks').fetchone()[0]
            while time.monotonic() < deadline:
                entries = o.call('thread/items/list', {'threadId': o.parent})['data']
                if any(e.get('item', {}).get('clientId') == client for e in entries):
                    break
                time.sleep(.05)
            else:
                raise AssertionError('native queue did not consume callback')
        os._exit(79)
o = Observer(s, os.environ["RECOVERY_PARENT"], fault=fault)
o.run()
s.close()
'''
            return subprocess.Popen([sys.executable, '-c', source], cwd=REPO,
                env=dict(env, RECOVERY_PARENT=parent, RECOVERY_FAULT=fault or '',
                         RECOVERY_QUEUE_BLOCK='1' if scenario == 'queue-restart' and fault else '0',
                         RECOVERY_AFTER_CONSUMED='1' if scenario == 'after-start' else '0'),
                stdin=subprocess.DEVNULL, stdout=observer_log, stderr=observer_log,
                start_new_session=True)

        try:
            native, rpc = start_native()
            parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
            origin = rpc.call('turn/start', {'threadId': parent,
                'input': [{'type': 'text', 'text': 'Run the finite command, then park.', 'text_elements': []}],
                'effort': 'high'})['turn']['id']
            report.update(parent=parent, origin=origin)
            if scenario == 'worker-restart':
                wait(lambda: provider.root_requests == 2 and provider.child_started.is_set(), 'real child did not spawn')
                children = rpc.call('thread/list', {'parentThreadId': parent, 'sourceKinds': ['subAgentThreadSpawn'],
                    'useStateDbOnly': True})['data']
                assert len(children) == 1, children
                process_id = children[0]['id']
                report['child'] = process_id
            else:
                wait(lambda: provider.count == 2, 'held command did not yield')
                terminals = rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']
                assert len(terminals) == 1, terminals
                process_id = terminals[0]['processId']
            report['process'] = process_id
            if scenario == 'native-replay':
                assert rpc.initialize_result.get('queueIdempotency') is True, 'native durable queue capability absent'
                key, cancelled_key = str(uuid.uuid4()), str(uuid.uuid4())
                params = {'threadId': parent, 'idempotencyKey': key, 'clientUserMessageId': key,
                          'input': [{'type': 'text', 'text': 'One native completion callback.', 'text_elements': []}]}
                receipt_params = {'threadId': parent, 'idempotencyKey': key}
                report['pending_add'] = rpc.call('thread/queue/add', params)
                assert report['pending_add']['receiptState'] == 'pending'
                queue_id = report['pending_add']['queuedSubmission']['id']
                report['pending_replay'] = rpc.call('thread/queue/add', params)
                assert report['pending_replay'] == report['pending_add'], 'pending replay changed submission'
                report['pending_receipt'] = rpc.call('thread/queue/receipt', receipt_params)
                assert report['pending_receipt']['receipt']['receiptState'] == 'pending'
                assert len(rpc.call('thread/queue/list', {'threadId': parent})['data']) == 1
                cancelled_params = {'threadId': parent, 'idempotencyKey': cancelled_key}
                assert rpc.call('thread/queue/receipt', cancelled_params)['receipt'] is None
                report['cancel_before_add'] = rpc.call('thread/queue/receipt/cancel', cancelled_params)
                assert report['cancel_before_add']['receipt'] == {'queuedSubmission': None, 'receiptState': 'cancelled'}
                cancelled_add = dict(params, idempotencyKey=cancelled_key, clientUserMessageId=cancelled_key)

                def rejected_late_add():
                    try:
                        rpc.call('thread/queue/add', cancelled_add)
                    except ValueError as exc:
                        assert 'cancelled' in str(exc), exc
                    else:
                        raise AssertionError('delayed enqueue bypassed cancellation tombstone')

                rejected_late_add()
                assert provider.count == 2, 'pending replay or cancellation invoked the model'
                provider.final_gate.set()
                wait(lambda: provider.count == 3, 'queued callback did not start once')
                wait(lambda: rpc.call('thread/queue/receipt', receipt_params)['receipt']['receiptState'] == 'consumed',
                     'callback consumption receipt did not become durable')
                report['consumed_replay'] = rpc.call('thread/queue/add', params)
                assert report['consumed_replay']['receiptState'] == 'consumed'
                assert report['consumed_replay']['queuedSubmission']['id'] == queue_id
                report['cancel_consumed'] = rpc.call('thread/queue/receipt/cancel', receipt_params)
                assert report['cancel_consumed']['receipt']['receiptState'] == 'consumed'
                release.touch()
                wait(lambda: rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']['status']['type'] == 'idle',
                     'native callback did not finish')
                rpc.close()
                stop(native)
                native, rpc = start_native()
                assert rpc.initialize_result.get('queueIdempotency') is True
                report['after_restart_thread'] = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
                assert report['after_restart_thread']['status']['type'] == 'notLoaded'
                report['restart_receipt'] = rpc.call('thread/queue/receipt', receipt_params)
                assert report['restart_receipt'] == report['cancel_consumed']
                report['restart_replay'] = rpc.call('thread/queue/add', params)
                assert report['restart_replay'] == report['consumed_replay']
                rejected_late_add()
                assert rpc.call('thread/queue/receipt/cancel', cancelled_params) == report['cancel_before_add']
                assert not rpc.call('thread/queue/list', {'threadId': parent})['data']
                receipts = [i for t in report['after_restart_thread']['turns'] for i in t['items']
                            if i.get('type') == 'userMessage' and i.get('clientId') == key]
                assert len(receipts) == 1, receipts
                time.sleep(.5)
                assert provider.count == 3, 'consumed replay or cancelled enqueue started another model turn'
                assert runs.read_text().splitlines() == ['run'], 'native replay reran initial shell command'
                report['passed'] = True
                print(json.dumps({'passed': True, 'scenario': scenario, 'loopback_provider_requests': provider.count,
                                  'remote_model_requests': 0}, indent=2))
                return
            store = Store(home)
            try:
                report['registration'] = store.register(rpc, parent,
                    [process_id] if scenario == 'worker-restart' else [],
                    [] if scenario == 'worker-restart' else [process_id], 'isolated recovery')
            finally:
                store.close()
            if scenario in ('shell-restart', 'worker-restart'):
                observer = start_observer()
            provider.final_gate.set()
            wait(lambda: rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']['status']['type']
                 == 'idle', 'parent did not become idle')
            time.sleep(1.2)
            initial_requests = 3 if scenario == 'worker-restart' else 2
            assert provider.count == initial_requests, 'parked parent made a polling model request'
            report['before_restart'] = coordinator_state(home)
            report['before_restart_thread'] = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
            if scenario not in ('shell-restart', 'worker-restart'):
                release.touch()
                wait(lambda: not rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data'],
                     'held command did not complete')
                fault = {'before-enqueue': 'before_enqueue', 'after-enqueue': 'after_enqueue',
                         'after-start': 'after_enqueue', 'queue-restart': 'after_enqueue'}[scenario]
                if scenario == 'queue-restart':
                    provider.hold_next = True
                observer = start_observer(fault)
                assert observer.wait(timeout=15) == 79, 'observer did not crash at requested delivery boundary'
                report['after_crash_state'] = coordinator_state(home)
                report['after_crash_queue'] = rpc.call('thread/queue/list', {'threadId': parent})
                if scenario == 'queue-restart':
                    assert provider.hold_started.wait(5), 'explicit extra turn did not reach held provider'
                    assert len(report['after_crash_queue']['data']) == 1, 'callback was not left pending'
                    queued = report['after_crash_queue']['data'][0]
                    replay_params = {'threadId': parent, 'idempotencyKey': queued['clientUserMessageId'],
                                     'clientUserMessageId': queued['clientUserMessageId'], 'input': queued['input']}
                    report['pending_replay'] = rpc.call('thread/queue/add', replay_params)
                    assert report['pending_replay']['receiptState'] == 'pending'
                    assert report['pending_replay']['queuedSubmission']['id'] == queued['id']
            if scenario in ('shell-restart', 'worker-restart', 'queue-restart'):
                if scenario != 'queue-restart':
                    assert observer.poll() is None, 'armed observer exited before restart'
                    os.kill(observer.pid, signal.SIGSTOP)
                rpc.close()
                stop(native)
                if scenario == 'queue-restart':
                    provider.hold_release.set()
                    wait(lambda: provider.count == 3, 'abandoned explicit turn request was not captured')
                native, rpc = start_native()
                report['after_restart_thread'] = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
                assert report['after_restart_thread']['status']['type'] == 'notLoaded'
                if scenario == 'worker-restart':
                    report['after_restart_child'] = rpc.call('thread/read', {'threadId': process_id, 'includeTurns': True})['thread']
                    assert report['after_restart_child']['status']['type'] == 'notLoaded'
                report['after_restart_turns'] = rpc.call('thread/turns/list', {'threadId': parent, 'itemsView': 'full'})
                for name, method in (('terminals', 'thread/backgroundTerminals/list'), ('queue', 'thread/queue/list')):
                    try:
                        report[f'after_restart_{name}'] = rpc.call(method, {'threadId': parent})
                    except ValueError as exc:
                        report[f'after_restart_{name}_error'] = str(exc)
                if scenario != 'queue-restart':
                    os.kill(observer.pid, signal.SIGCONT)
                    report['observer_reconnected_pid'] = observer.pid
                else:
                    assert rpc.initialize_result.get('queueIdempotency') is True
                    report['restart_pending_replay'] = rpc.call('thread/queue/add', replay_params)
                    assert report['restart_pending_replay'] == report['pending_replay']
                    assert provider.count == 3, 'pending replay after restart invoked the model'
            if scenario not in ('shell-restart', 'worker-restart'):
                observer = start_observer()
            time.sleep(5)
            report['reconnected_state'] = coordinator_state(home)
            report['model_requests_during_recovery'] = provider.count
            report['shell_runs'] = runs.read_text().splitlines() if runs.exists() else []
            expected_requests = initial_requests + 1 + (1 if scenario == 'queue-restart' else 0)
            assert provider.count <= expected_requests, 'restart produced duplicate completion turns'
            assert report['shell_runs'] == ([] if scenario == 'worker-restart' else ['run']), 'recovery reran the shell command'
            terminal = ('interrupted', 'failed', 'lost', 'unknown') if scenario in ('shell-restart', 'worker-restart') else ('completed',)
            wait(lambda: coordinator_state(home)['jobs'][0]['status'] in terminal,
                 'restarted daemon did not reconcile job terminal outcome', timeout=6)
            wait(lambda: provider.count == expected_requests, 'recovery did not deliver one job completion', timeout=6)
            wait(lambda: coordinator_state(home)['callbacks'] and
                 coordinator_state(home)['callbacks'][0]['state'] == 'delivered',
                 'restart completion was not reconciled as delivered', timeout=6)
            report['final_state'] = coordinator_state(home)
            report['final_thread'] = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
            assert len(report['final_thread']['turns']) == (3 if scenario == 'queue-restart' else 2), report['final_thread']
            callbacks = report['final_state']['callbacks']
            assert len(callbacks) == 1, callbacks
            receipts = [i for t in report['final_thread']['turns'] for i in t['items']
                        if i.get('type') == 'userMessage' and i.get('clientId') == callbacks[0]['client']]
            assert len(receipts) == 1, receipts
            if scenario in ('shell-restart', 'worker-restart'):
                assert 'status=completed' not in json.dumps(receipts), 'lost work was reported as completed'
            assert not rpc.call('thread/queue/list', {'threadId': parent})['data']
            if observer:
                assert observer.wait(timeout=6) == 0, 'observer did not finish after reconciled delivery'
            observer = start_observer()
            assert observer.wait(timeout=6) == 0, 'restarted completed observer did not exit'
            time.sleep(.5)
            assert provider.count == expected_requests, provider.requests
            report['passed'] = True
        finally:
            provider.final_gate.set()
            if isinstance(provider, WorkerProvider):
                provider.child_gate.set()
            else:
                provider.hold_release.set()
            release.touch()
            if rpc and native and native.poll() is None and 'parent' in report:
                try:
                    report['cleanup_thread'] = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
                    report['cleanup_queue'] = rpc.call('thread/queue/list', {'threadId': parent})
                except (OSError, ValueError) as exc:
                    report['cleanup_rpc_error'] = str(exc)
            if rpc:
                rpc.close()
            if native:
                stop(native)
            if observer:
                stop(observer)
            stop_observers(home)
            native_log.close()
            observer_log.close()
            for path in home.glob('sessions/**/*.jsonl'):
                shutil.copy2(path, evidence / path.name)
            report['loopback_provider_requests'] = provider.count
            if (home / 'codex-plus' / 'background.sqlite').exists():
                report['cleanup_state'] = coordinator_state(home)
            (evidence / 'provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            (evidence / 'rpc-notifications.json').write_text(json.dumps(rpc.messages if rpc else [], indent=2) + '\n')
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            provider.close()
    print(json.dumps({key: report[key] for key in ('passed', 'parent', 'process',
          'loopback_provider_requests', 'remote_model_requests')}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, default=REPO / '.build' / 'wake-recovery')
    parser.add_argument('--scenario', choices=SCENARIOS + ('all',), default='all')
    args = parser.parse_args()
    for scenario in SCENARIOS if args.scenario == 'all' else (args.scenario,):
        run(args.binary.resolve(), args.evidence.resolve() / scenario, scenario)
