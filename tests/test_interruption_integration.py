"""Private stock-daemon regression for ambiguous origin and worker projections.

The adapter changes only exact selected turn projections; ordinary RPC responses,
native workers, completion persistence, and callback delivery use the real daemon.
"""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from test_background_integration import (
    REPO, RecordingClient, assert_short_callback, coordinator_state, stop, wait,
)
from codex_plus.background import Observer, Store, register, turn
from fixtures.interruption_provider import InterruptionProvider


class ProjectionClient:
    def __init__(self, client, faults, unavailable_path):
        self.client = client
        self.faults = faults
        self.records = []
        self.calls = []
        self.unavailable_path = unavailable_path
        self.generation = client.generation
        self.initialize_result = client.initialize_result

    def call(self, method, params):
        result = self.client.call(method, params)
        self.calls.append({'method': method, 'params': params})
        if method == 'thread/read' and self.unavailable_path is not None and any(
                thread == params['threadId'] for thread, _ in self.faults):
            result = deepcopy(result)
            canonical_path = result['thread'].get('path')
            result['thread']['path'] = str(self.unavailable_path)
            self.records.append({'thread': params['threadId'], 'canonical_path': canonical_path,
                                 'injected_unavailable_path': str(self.unavailable_path)})
        if method == 'thread/turns/list':
            for index, row in enumerate(result['data']):
                key = (params['threadId'], row['id'])
                if key in self.faults:
                    canonical = deepcopy(row)
                    result = deepcopy(result)
                    result['data'][index].update(status='interrupted', completedAt=None)
                    self.records.append({'thread': key[0], 'turn': key[1],
                                         'canonical': canonical, 'injected': result['data'][index]})
        return result

    def close(self):
        pass  # The fixture owns the shared connection.


def fake_regression():
    """Small desired-behavior check also usable before the real fixture is ready."""
    from test_background import Fake, PARENT, CHILD
    with tempfile.TemporaryDirectory(prefix='codex-plus-interruption-red-') as home:
        fake = Fake()
        store = Store(home)
        try:
            register(store, fake, PARENT, [CHILD], [], 'projection regression')
            observer = Observer(store, PARENT, lambda: fake)
            observer.step()
            fake.parent_status = 'idle'
            fake.parent_turns[0].update(status='interrupted', completedAt=None)
            observer.step()
            state = store.status(PARENT)
            assert state['parks'][0]['state'] == 'armed', state
            assert not state['callbacks'] and not fake.starts, state
            fake.idle()
            fake.complete()
            observer.step()
            assert store.status(PARENT)['jobs'][0]['status'] == 'completed'
            assert len(fake.starts) == 1
        finally:
            store.close()


def run(binary, evidence, scenario):
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'scenario': scenario, 'binary': str(binary),
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'binary_version': subprocess.check_output([str(binary), '--version'], text=True).strip(),
              'remote_model_requests': 0,
              'fault_boundary': 'RPC adapter, never the daemon or persisted rollout'}
    provider = InterruptionProvider()
    active_started, active_release = threading.Event(), threading.Event()
    original_handler = provider.server.RequestHandlerClass

    class HeldResponse(original_handler):
        def do_POST(self):
            if (provider.root_requests == 2 and scenario in ('active-parent', 'foreign-queue')
                    and provider.child_gate.is_set()):
                active_started.set()
                if not active_release.wait(20):
                    self.send_error(500, 'active-parent response hold timed out')
                    return
            super().do_POST()

    provider.server.RequestHandlerClass = HeldResponse
    native = rpc = store = adapter = None
    # Keep the private Unix socket path below the platform's pathname limit.
    with tempfile.TemporaryDirectory(prefix='codex-plus-interruption-') as temporary:
        root = Path(temporary)
        home, workspace = root / 'home', root / 'workspace'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        env.update(CODEX_HOME=str(home), PYTHONPATH=str(REPO), RUST_LOG='warn')
        (home / 'config.toml').write_text(f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
analytics.enabled=false
features.daemon_auto_start=false
features.plugins=false
features.multi_agent=true
[model_providers.isolated]
name="loopback interruption acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
        native_log = (evidence / 'native.stderr').open('w')
        try:
            native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
                stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log, start_new_session=True)

            def connect():
                assert native.poll() is None, 'private stock daemon exited'
                try:
                    return RecordingClient(home / 'app-server-control' / 'app-server-control.sock')
                except (OSError, ValueError):
                    return None

            rpc = wait(connect, 'private daemon did not accept RPC')
            assert not rpc.initialize_result.get('queueIdempotency'), 'requires official stock daemon'
            parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
            origin = rpc.call('turn/start', {'threadId': parent,
                'input': [{'type': 'text', 'text': 'Spawn child, then park.', 'text_elements': []}],
                'effort': 'high'})['turn']['id']
            wait(lambda: provider.root_requests == 2 and provider.child_started.is_set(), 'native child did not spawn')
            children = rpc.call('thread/list', {'parentThreadId': parent,
                'sourceKinds': ['subAgentThreadSpawn'], 'useStateDbOnly': True})['data']
            assert len(children) == 1, children
            child = children[0]['id']
            store = Store(home)
            report['registration'] = register(store, rpc, parent, [child], [], 'projection acceptance')
            worker_turn = store.status(parent)['jobs'][0]['turn']
            report.update(parent=parent, origin=origin, child=child, worker_turn=worker_turn)
            faults = set()
            adapter = ProjectionClient(rpc, faults, None if scenario in ('durable-evidence', 'durable-abort') else
                                       home / 'sessions' / 'withheld-evidence.jsonl')
            observer = Observer(store, parent, lambda: adapter)

            def step():
                result = observer.step()
                store.monitoring_succeeded(parent)
                store.publish(parent)
                return result

            def restart_observer():
                nonlocal store, observer
                store.close()
                store = Store(home)
                observer = Observer(store, parent, lambda: adapter)
                observer.recover()
                report.setdefault('observer_restart_states', []).append(store.status(parent))

            step()  # Establish observer ownership while both real turns run.
            if scenario in ('abort', 'durable-abort'):
                rpc.call('turn/interrupt', {'threadId': parent, 'turnId': origin})
                wait(lambda: turn(rpc, parent, origin)['status'] == 'interrupted', 'real origin did not abort')
                report['real_aborted_origin'] = turn(rpc, parent, origin)
                if scenario == 'durable-abort':
                    faults.add((parent, origin))

                def cancelled():
                    step()
                    return store.status(parent)['parks'][0]['state'] == 'cancelled'

                wait(cancelled, 'positive real abort evidence did not cancel registration')
                assert store.status(parent)['parks'][0]['state'] == 'cancelled', store.status(parent)
                if scenario == 'durable-abort':
                    evidence_record = json.loads(store.status(parent)['parks'][0]['cancel_evidence'])
                    assert evidence_record['source'] == 'rollout' and evidence_record['event'] == 'turn_aborted'
            else:
                provider.final_gate.set()
                wait(lambda: turn(rpc, parent, origin)['status'] == 'completed', 'parent did not finish')
                assert rpc.call('thread/read', {'threadId': parent})['thread']['status']['type'] == 'idle'
                if scenario != 'worker-projection':
                    faults.add((parent, origin))
                else:
                    faults.add((child, worker_turn))
                if scenario == 'restart-pending':
                    faults.add((child, worker_turn))
                before = provider.count
                step()
                report['pending_state'] = store.status(parent)
                assert report['pending_state']['parks'][0]['state'] == 'armed', report['pending_state']
                assert report['pending_state']['jobs'][0]['status'] == 'armed', report['pending_state']
                assert not report['pending_state']['callbacks'], report['pending_state']
                assert adapter.records, 'intentional projection fault was never consumed'
                if scenario != 'durable-evidence':
                    assert report['pending_state']['health']['state'] == 'error', report['pending_state']
                time.sleep(.2)
                assert provider.count == before == 3, 'observer waiting invoked provider'
                report['waiting_provider_requests'] = {'before': before, 'after': provider.count}
                if scenario == 'cancel':
                    store.request_cancel(parent)
                if scenario in ('restart-pending', 'restart-completed'):
                    restart_observer()
                    step()
                    assert store.status(parent)['parks'][0]['state'] == 'armed'
                provider.child_gate.set()
                wait(lambda: turn(rpc, child, worker_turn)['status'] == 'completed', 'original native worker did not finish')
                report['canonical_completed_worker'] = turn(rpc, child, worker_turn)
                if scenario == 'worker-projection':
                    step()
                    assert store.status(parent)['jobs'][0]['status'] == 'armed'
                    assert provider.count == before, 'ambiguous worker caused callback'
                if scenario == 'restart-completed':
                    # Observe the real terminal outcome while origin uncertainty blocks delivery.
                    step()
                    report['completed_before_restart'] = store.status(parent)
                    assert report['completed_before_restart']['jobs'][0]['status'] == 'completed'
                    restart_observer()
                faults.clear()
                if scenario == 'reuse':
                    provider.child_gate.clear()
                    provider.reuse_next = True
                    reuse_origin = rpc.call('turn/start', {'threadId': parent,
                        'input': [{'type': 'text', 'text': 'Reuse the native child.', 'text_elements': []}]})['turn']['id']
                    wait(lambda: provider.count == before + 3, 'native followup_task did not start later child')
                    wait(lambda: turn(rpc, parent, reuse_origin)['status'] == 'completed', 'reuse parent did not finish')
                    reused = rpc.call('thread/turns/list', {'threadId': child, 'limit': 1,
                        'sortDirection': 'desc', 'itemsView': 'notLoaded'})['data'][0]['id']
                    assert reused != worker_turn
                    assert turn(rpc, child, reused)['status'] == 'inProgress'
                    report['later_worker_turn'] = reused
                if scenario == 'active-parent':
                    active = rpc.call('turn/start', {'threadId': parent,
                        'input': [{'type': 'text', 'text': 'Held user turn.', 'text_elements': []}]})['turn']['id']
                    assert active_started.wait(5), 'parent request did not reach explicit hold'
                    step()
                    assert provider.count == before, 'active parent gate invoked provider'
                    assert not any(c['method'] == 'thread/queue/start' for c in adapter.calls)
                    active_release.set()
                    wait(lambda: turn(rpc, parent, active)['status'] == 'completed', 'held parent did not finish')
                foreign = None
                if scenario == 'foreign-queue':
                    rpc.call('thread/queue/add', {'threadId': parent,
                        'clientUserMessageId': str(uuid.uuid4()),
                        'input': [{'type': 'text', 'text': 'Held user queue turn.', 'text_elements': []}]})
                    assert active_started.wait(5), 'stock queue did not start held user turn'
                    foreign = rpc.call('thread/queue/add', {'threadId': parent,
                        'clientUserMessageId': str(uuid.uuid4()),
                        'input': [{'type': 'text', 'text': 'User queue entry.', 'text_elements': []}]})['queuedSubmission']['id']
                    step()
                    assert provider.count == before, 'foreign queue gate invoked provider'
                    assert not any(c['method'] == 'thread/queue/start' for c in adapter.calls)
                    report['foreign_queue_while_user_active'] = rpc.call('thread/queue/list', {'threadId': parent})
                    assert any(row['id'] == foreign for row in report['foreign_queue_while_user_active']['data'])
                    rpc.call('thread/queue/delete', {'threadId': parent, 'queuedSubmissionId': foreign})
                    active_release.set()
                    wait(lambda: rpc.call('thread/read', {'threadId': parent})['thread']['status']['type'] == 'idle',
                         'held user queue turn did not finish')
                step()
                if scenario != 'cancel':
                    expected = before + 1 + (3 if scenario == 'reuse' else
                                             scenario in ('active-parent', 'foreign-queue'))
                    wait(lambda: provider.count == expected, 'exactly one completion callback did not start')
                    wait(lambda: rpc.call('thread/read', {'threadId': parent})['thread']['status']['type'] == 'idle',
                         'callback did not finish')
                    step()
                    final = store.status(parent)
                    assert len(final['callbacks']) == 1 and final['callbacks'][0]['state'] == 'delivered', final
                    assert final['jobs'][0]['turn'] == worker_turn and final['jobs'][0]['status'] == 'completed', final
                    thread = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
                    callback = final['callbacks'][0]
                    receipts = [i for t in thread['turns'] for i in t['items']
                                if i.get('type') == 'userMessage' and i.get('clientId') == callback['client']]
                    assert len(receipts) == 1, receipts
                    assert_short_callback(callback, receipts[0], parent, final['jobs'][0]['label'],
                                          report_only=scenario in ('restart-pending', 'restart-completed'))
                    restart_observer()
                    step()
                    assert store.status(parent)['jobs'][0]['status'] == 'completed'
                    time.sleep(.2)
                    assert provider.count == expected, 'restart duplicated callback'
            if scenario in ('abort', 'durable-abort', 'cancel'):
                provider.final_gate.set()
                provider.child_gate.set()
                time.sleep(.2)
                step()
                assert not store.status(parent)['callbacks'], store.status(parent)
                assert not any(c['method'] in ('thread/queue/add', 'thread/queue/start', 'thread/resume')
                               for c in adapter.calls), adapter.calls
                assert provider.root_requests == 2, 'cancelled park woke parent'
            report['final_state'] = store.status(parent)
            report['passed'] = True
        finally:
            active_release.set()
            provider.final_gate.set()
            provider.child_gate.set()
            if adapter:
                report['projection_faults'] = adapter.records
                report['observer_rpc_calls'] = adapter.calls
            if store:
                report['cleanup_state'] = coordinator_state(home)
                store.close()
            if rpc:
                rpc.close()
            if native:
                stop(native)
            native_log.close()
            for path in home.glob('sessions/**/*.jsonl'):
                shutil.copy2(path, evidence / path.name)
            report['loopback_provider_requests'] = provider.count
            (evidence / 'provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            provider.close()
    print(json.dumps({key: report[key] for key in ('scenario', 'passed', 'loopback_provider_requests')}))


SCENARIOS = ('origin-projection', 'worker-projection', 'durable-evidence', 'restart-pending',
             'restart-completed', 'reuse', 'foreign-queue', 'active-parent', 'abort', 'durable-abort', 'cancel')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path, nargs='?')
    parser.add_argument('--evidence', type=Path, default=REPO / '.build/wake-fix/integration')
    parser.add_argument('--scenario', choices=SCENARIOS + ('all', 'fake'), default='all')
    args = parser.parse_args()
    if args.scenario == 'fake':
        fake_regression()
    else:
        if args.binary is None:
            parser.error('binary is required for stock integration')
        for scenario in SCENARIOS if args.scenario == 'all' else (args.scenario,):
            run(args.binary.resolve(), args.evidence.resolve() / scenario, scenario)
