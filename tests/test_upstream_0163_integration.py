"""Private stock-daemon upstream boundary checks; all provider traffic is loopback.

python3 tests/test_upstream_0163_integration.py /absolute/official/codex --evidence PATH
Optional: --rollback-binary /absolute/official-0.162.1/codex --scenario rollback-read
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time

from test_background_integration import REPO, RecordingClient, stop, stop_observers, wait
from codex_plus.background import Observer, Store, classify_turn, register, turn
from fixtures.upstream_0163 import BoundaryProvider, execute, function, message


class PrivateServer:
    def __init__(self, binary, evidence, v2=False):
        self.binary, self.evidence = binary, evidence
        evidence.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='u163-')
        self.root = Path(self.temporary.name)
        self.home, self.work = self.root / 'home', self.root / 'work'
        self.home.mkdir(mode=0o700)
        self.work.mkdir()
        self.release, self.runs = self.work / 'release', self.work / 'runs'
        self.provider = BoundaryProvider()
        self.env = {k: v for k, v in os.environ.items()
                    if not any(s in k.upper() for s in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        self.env.update(CODEX_HOME=str(self.home), PYTHONPATH=str(REPO), RUST_LOG='warn')
        (self.home / 'config.toml').write_text(f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
analytics.enabled=false
thread_unload_delay_secs=1
features.daemon_auto_start=false
features.plugins=false
features.multi_agent=true
features.multi_agent_v2={str(v2).lower()}
features.code_mode=true
features.unified_exec=true
features.shell_tool=true
[model_providers.isolated]
name="loopback upstream acceptance"
base_url="{self.provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
        self.log = (evidence / 'native.stderr').open('w')
        self.native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=self.env,
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log, start_new_session=True)
        try:
            self.rpc = wait(self.connect, 'private daemon did not accept RPC')
            assert not self.rpc.initialize_result.get('queueIdempotency'), 'requires stock daemon'
            self.parent = self.rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(self.work),
                'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
        except Exception:
            stop(self.native)
            self.provider.close()
            self.log.close()
            self.temporary.cleanup()
            raise
        self.provider.parent = self.parent
        self.store = Store(self.home)
        self.observer = Observer(self.store, self.parent, lambda: self.rpc)
        self.report = dict(binary=str(binary), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                           binary_version=subprocess.check_output([str(binary), '--version'], text=True).strip(),
                           parent=self.parent, remote_model_requests=0, code_mode_interrupt_override=False)

    def connect(self):
        assert self.native.poll() is None, 'private daemon exited; inspect native.stderr'
        try:
            return RecordingClient(self.home / 'app-server-control/app-server-control.sock')
        except (OSError, ValueError):
            return None

    def start(self, text='exercise upstream boundary'):
        return self.rpc.call('turn/start', {'threadId': self.parent,
            'input': [{'type': 'text', 'text': text, 'textElements': []}], 'effort': 'high'})['turn']['id']

    def thread(self, ident=None):
        return self.rpc.call('thread/read', {'threadId': ident or self.parent})['thread']

    def terminal(self, origin, thread=None):
        def completed():
            current = turn(self.rpc, thread or self.parent, origin)
            status, evidence = classify_turn(self.rpc, self.home, thread or self.parent, current)
            if status in ('completed', 'failed', 'interrupted'):
                return dict(current, status=status, terminalEvidence=evidence)
        return wait(completed, 'exact turn did not finish')

    def state(self):
        return self.store.status(self.parent)

    def poll(self):
        self.observer.step()
        return self.state()

    def managed_command(self):
        script = (f'from pathlib import Path; import time; p=Path({str(self.release)!r}); '
                  f'Path({str(self.runs)!r}).open("a").write("run\\n"); '
                  'deadline=time.monotonic()+40\n'
                  'while not p.exists() and time.monotonic()<deadline: time.sleep(.05)\n'
                  'assert p.exists(), "fixture release timeout"\nprint("boundary-complete")')
        return '# codex-plus-shell: auto\n' + shlex.join(
            [str(REPO / 'bin/codex-plus'), 'run', '--', 'python3', '-c', script])

    def terminals(self):
        return self.rpc.call('thread/backgroundTerminals/list', {'threadId': self.parent})['data']

    def process(self):
        return wait(lambda: (ts := self.terminals()) and ts[0]['processId'], 'managed shell did not start')

    def command_item(self, process):
        data = self.rpc.call('thread/items/list', {'threadId': self.parent})['data']
        return next((e for e in data if e['item'].get('type') == 'commandExecution'
                     and e['item'].get('processId') == process), None)

    def close(self, passed):
        self.release.touch()
        self.provider.close()
        self.report.update(passed=passed, provider_requests=self.provider.requests,
                           coordinator=self.state(), rpc_messages=self.rpc.messages)
        self.store.close()
        self.rpc.close()
        stop(self.native)
        stop_observers(self.home)
        self.log.close()
        for source in self.home.glob('sessions/**/*.jsonl'):
            shutil.copy2(source, self.evidence / source.name)
        for source in (self.home / 'codex-plus/logs').glob('*.log'):
            shutil.copy2(source, self.evidence / ('managed-' + source.name))
        (self.evidence / 'report.json').write_text(json.dumps(self.report, indent=2) + '\n')
        self.temporary.cleanup()


def pending_origin(f):
    arguments = dict(cmd=f.managed_command(), yield_time_ms=10000, max_output_tokens=100, login=False)
    f.provider.root_steps.append((execute('text(await tools.exec_command(' + json.dumps(arguments) + '));'), None))
    origin = f.start()
    process = f.process()
    wait(lambda: f.runs.exists(), 'managed command did not execute')
    register(f.store, f.rpc, f.parent, [], [process], 'pending nested origin interrupt')
    f.rpc.call('turn/interrupt', {'threadId': f.parent, 'turnId': origin})
    assert f.terminal(origin)['status'] == 'interrupted'
    f.poll()
    assert f.state()['parks'][0]['state'] == 'cancelled', f.state()
    f.release.touch()
    wait(lambda: not f.terminals(), 'released command stayed running')
    f.poll()
    assert not f.state()['callbacks'], f.state()
    assert f.runs.read_text() == 'run\n', 'managed command replayed'
    f.report.update(origin=origin, process=process, actual_command_item=f.command_item(process))


def older_cell(f):
    gate = f.provider.gate()
    arguments = dict(cmd=f.managed_command(), yield_time_ms=1000, max_output_tokens=100, login=False)
    code = ('const result = await tools.exec_command(' + json.dumps(arguments) + '); text(result); '
            'yield_control(); await new Promise(() => {});')
    f.provider.root_steps.extend([(execute(code), None), (message('parked'), gate)])
    origin = f.start()
    process = f.process()
    wait(lambda: f.provider.count == 2, 'older code-mode cell did not yield')
    cell = re.search(r'Script running with cell ID ([\w-]+)', json.dumps(f.provider.requests[-1]['body']))
    assert cell, f.provider.requests[-1]
    cell = cell.group(1)
    register(f.store, f.rpc, f.parent, [], [process], 'older completed origin')
    gate.set()
    assert f.terminal(origin)['status'] == 'completed'
    later_gate = f.provider.gate()
    f.provider.root_steps.append((message('later turn'), later_gate))
    later = f.start('unrelated later root turn')
    wait(lambda: f.provider.count == 3, 'later root turn not held')
    f.rpc.call('turn/interrupt', {'threadId': f.parent, 'turnId': later})
    assert f.terminal(later)['status'] == 'interrupted'
    f.poll()
    assert f.state()['parks'][0]['state'] == 'armed', f.state()
    later_gate.set()
    f.provider.root_steps.extend([(function('wait', {'cell_id': cell, 'yield_time_ms': 1}, 'check-old-cell'), None),
                                  (message('checked cancellation'), None)])
    check = f.start('inspect interrupted older cell')
    f.terminal(check)
    output = json.dumps(f.provider.requests[-1]['body'])
    assert 'not found' in output and 'check-old-cell' in output, 'old cell wait lacks not-found output'
    before = f.provider.count
    f.release.touch()
    wait(lambda: not f.terminals(), 'released older command stayed running')
    wait(lambda: f.poll()['callbacks'] and f.state()['callbacks'][0]['state'] == 'delivered',
         'completion did not produce one callback')
    wait(lambda: f.thread()['status']['type'] == 'idle', 'callback did not finish')
    assert f.provider.count == before + 1 and len(f.state()['callbacks']) == 1, f.state()
    assert f.runs.read_text() == 'run\n', 'older shell replayed'
    f.report.update(origin=origin, later=later, cell=cell, process=process,
                    actual_command_item=f.command_item(process))


def spawn_worker(f):
    root_gate, child_gate = f.provider.gate(), f.provider.gate()
    f.provider.child_gates.append(child_gate)
    spawn = function('spawn_agent', dict(task_name='held', message='held child boundary', fork_turns='none'),
                     'spawn-held', 'collaboration')
    f.provider.root_steps.extend([(spawn, None), (message('parked'), root_gate)])
    origin = f.start()
    wait(lambda: f.provider.count >= 3, 'native worker did not start')
    children = f.rpc.call('thread/list', {'parentThreadId': f.parent, 'sourceKinds': ['subAgentThreadSpawn'],
                                         'useStateDbOnly': True})['data']
    assert len(children) == 1, children
    child = children[0]['id']
    register(f.store, f.rpc, f.parent, [child], [], 'registered native worker')
    return origin, child, root_gate, child_gate


def worker_origin(f):
    origin, child, root_gate, child_gate = spawn_worker(f)
    watched = f.state()['jobs'][0]['turn']
    f.rpc.call('turn/interrupt', {'threadId': f.parent, 'turnId': origin})
    assert f.terminal(origin)['status'] == 'interrupted'
    f.poll()
    assert f.state()['parks'][0]['state'] == 'cancelled', f.state()
    root_gate.set()
    child_gate.set()
    terminal = f.terminal(watched, child)
    f.poll()
    assert not f.state()['callbacks'], f.state()
    f.report.update(origin=origin, child=child, watched=watched, actual_worker_terminal=terminal)


def v2_reuse(f):
    origin, child, root_gate, child_gate = spawn_worker(f)
    watched = f.state()['jobs'][0]['turn']
    before = f.thread(child)
    f.rpc.call('thread/unsubscribe', {'threadId': child})
    root_gate.set()
    f.terminal(origin)
    child_gate.set()
    assert f.terminal(watched, child)['status'] == 'completed'
    wait(lambda: f.thread(child)['status']['type'] == 'notLoaded', 'V2 child was not evicted')
    evicted = f.thread(child)
    for key in ('id', 'parentThreadId', 'source', 'model', 'reasoningEffort'):
        assert evicted[key] == before[key], (key, before, evicted)
    snapshot = f.provider.count
    f.observer.claim_jobs()
    f.observer.cancel_interrupted()
    f.observer.poll_jobs()
    assert f.state()['jobs'][0]['status'] == 'completed', f.state()
    assert f.provider.count == snapshot, 'read-only observation called the provider'
    # Rehydrate through the native follow-up tool, then watch its distinct new turn.
    next_child, next_root = f.provider.gate(), f.provider.gate()
    f.provider.child_gates.append(next_child)
    f.provider.root_steps.extend([(function('followup_task', {'target': 'held', 'message': 'new exact turn'},
                                           'reuse-held', 'collaboration'), None),
                                  (message('reused worker parked'), next_root)])
    wait(lambda: f.thread()['status']['type'] == 'idle', 'root not idle before followup')
    new_origin = f.start('rehydrate existing child')
    wait(lambda: sum(r['child'] for r in f.provider.requests) == 2, 'child was not rehydrated')
    current = wait(lambda: (t := f.rpc.call('thread/turns/list', {'threadId': child, 'limit': 1})['data'][0])
                   and t['id'] != watched and t, 'rehydration reused watched turn')
    register(f.store, f.rpc, f.parent, ['/root/held'], [], 'new child turn only')
    f.observer.poll_jobs()
    jobs = {j['turn']: j for j in f.state()['jobs']}
    assert jobs[watched]['status'] == 'completed' and jobs[current['id']]['status'] == 'armed', jobs
    next_root.set()
    f.terminal(new_origin)
    next_child.set()
    assert f.terminal(current['id'], child)['status'] == 'completed'
    wait(lambda: f.thread()['status']['type'] == 'idle', 'V2 parent completion mail did not settle')
    wait(lambda: len(f.poll()['callbacks']) == 2 and all(c['state'] == 'delivered' for c in f.state()['callbacks']),
         'V2 callback did not deliver')
    assert all(j['status'] == 'completed' for j in f.state()['jobs']), f.state()
    assert len(f.state()['callbacks']) == 2, f.state()
    f.report.update(origin=origin, child=child, watched=watched, evicted_metadata=evicted,
                    reused_turn=current['id'], reused_metadata=f.thread(child))


def rollback_read(f, binary):
    origin = f.start('create representative history before rollback')
    wait(lambda: f.provider.count == 1 and f.thread()['status']['type'] == 'idle',
         'representative history was not materialized')
    assert f.terminal(origin)['status'] == 'completed'
    f.rpc.call('thread/unsubscribe', {'threadId': f.parent})
    wait(lambda: f.thread()['status']['type'] == 'notLoaded', 'private root did not unload')
    client = 'upstream-rollback-probe'
    queued = f.rpc.call('thread/queue/add', {'threadId': f.parent, 'clientUserMessageId': client,
        'input': [{'type': 'text', 'text': 'leave queued, do not run', 'textElements': []}]})['queuedSubmission']
    assert f.rpc.call('thread/queue/list', {'threadId': f.parent})['data'] == [queued]
    f.rpc.close()
    stop(f.native)
    shutil.copytree(f.home, f.root / 'pre-rollback-home')
    log_path = f.evidence / 'rollback-native.stderr'
    with log_path.open('w') as log:
        env = dict(f.env)
        native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        rpc = None
        outcome = dict(binary=str(binary), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                       binary_version=subprocess.check_output([str(binary), '--version'], text=True).strip(),
                       private_home=True, backup_before_downgrade=True, startup=False, readable=False)
        try:
            deadline = time.monotonic() + 15
            while native.poll() is None and time.monotonic() < deadline:
                try:
                    rpc = RecordingClient(f.home / 'app-server-control/app-server-control.sock')
                    break
                except (OSError, ValueError):
                    time.sleep(.05)
            outcome['startup'] = rpc is not None
            if rpc:
                try:
                    outcome['thread'] = rpc.call('thread/read', {'threadId': f.parent})
                    outcome['turns'] = rpc.call('thread/turns/list', {'threadId': f.parent})
                    outcome['queue'] = rpc.call('thread/queue/list', {'threadId': f.parent})
                    outcome['readable'] = (any(t['id'] == origin and t['status'] == 'completed'
                                             for t in outcome['turns']['data'])
                                           and any(q['id'] == queued['id'] and q['clientUserMessageId'] == client
                                                   for q in outcome['queue']['data']))
                except ValueError as exc:
                    outcome['rpc_error'] = str(exc)
            outcome['exit_code_before_cleanup'] = native.poll()
        finally:
            if rpc:
                rpc.close()
            stop(native)
        outcome['stderr'] = log_path.read_text()
        f.report['rollback'] = outcome
        # A failed downgrade is evidence of a release limit, not a reason to alter upstream state.
        assert outcome['startup'] and outcome['readable'], 'old daemon cannot read new private state; see rollback report'


CASES = {'pending-nested-origin-abort': pending_origin, 'older-cell-later-interrupt': older_cell,
         'registered-worker-origin-abort': worker_origin, 'v2-eviction-reuse': v2_reuse}


def run(binary, evidence, scenario, rollback_binary=None):
    f = PrivateServer(binary, evidence, v2=scenario == 'v2-eviction-reuse')
    passed = False
    try:
        if scenario == 'rollback-read':
            rollback_read(f, rollback_binary)
        else:
            CASES[scenario](f)
        passed = True
    finally:
        f.close(passed)
    print(json.dumps(dict(scenario=scenario, passed=True)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--scenario', choices=tuple(CASES) + ('rollback-read', 'all'), default='all')
    parser.add_argument('--rollback-binary', type=Path)
    args = parser.parse_args()
    if args.scenario == 'rollback-read' and args.rollback_binary is None:
        parser.error('--rollback-binary is required for rollback-read')
    for scenario in CASES if args.scenario == 'all' else (args.scenario,):
        run(args.binary.resolve(), args.evidence.resolve() / scenario, scenario,
            args.rollback_binary.resolve() if args.rollback_binary else None)
