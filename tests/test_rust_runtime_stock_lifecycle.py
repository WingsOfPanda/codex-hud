"""Rust observation through real official-daemon interruptions and restarts.

Reuse the existing upstream fixture/provider; all CLI helpers are the copied Rust
runtime and every provider request is loopback. Python remains only the driver.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time

from test_background_integration import stop, wait
from test_rust_runtime_stock_integration import stop_rust_observers
from test_upstream_0163_integration import PrivateServer, spawn_worker
from fixtures.upstream_0163 import execute, function, message

SCENARIOS = ('observer-completed', 'observer-running', 'daemon-restart', 'origin-abort', 'worker-reuse')


def run_worker(runtime, binary, evidence):
    """Native V2 rehydration gives one child thread two independently watched turns."""
    f = PrivateServer(binary, evidence, v2=True)
    staged = f.home / 'runtime-package/bin/codex-plus-runtime'
    staged.parent.mkdir(parents=True)
    shutil.copy2(runtime, staged)
    lock = (f.home / 'codex-plus' / f'{f.parent}.supervisor.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    observers = []
    passed = False

    def cli(*args):
        result = subprocess.run([str(staged), *args, '--parent', f.parent], env=f.env,
                                capture_output=True, text=True, timeout=12)
        assert result.returncode == 0, (args, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def observe():
        log = (evidence / f'observer-{len(observers)}.log').open('w')
        process = subprocess.Popen([str(staged), 'observe', '--parent', f.parent], env=f.env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        observers.append((process, log))
        return process

    def delivered(count):
        rows = f.state()['callbacks']
        return len(rows) == count and all(c['state'] == 'delivered' for c in rows)

    try:
        origin, child, root_gate, child_gate = spawn_worker(f)
        watched = f.state()['jobs'][0]['turn']
        # The existing fixture's real 0.2 registration is reopened through the Rust CLI.
        assert cli('park', '--worker', child, '--reason', 'cross-language native worker')['state'] == 'already_registered'
        first = observe()
        wait(lambda: f.state()['jobs'][0]['observer_run'], 'Rust did not claim actual V2 worker')
        root_gate.set()
        assert f.terminal(origin)['status'] == 'completed'
        child_gate.set()
        assert f.terminal(watched, child)['status'] == 'completed'
        wait(lambda: delivered(1), 'Rust did not deliver first native worker callback')
        wait(lambda: f.thread()['status']['type'] == 'idle', 'root callback did not settle')
        first.wait(timeout=6)
        f.rpc.call('thread/unsubscribe', {'threadId': child})
        wait(lambda: f.thread(child)['status']['type'] == 'notLoaded', 'completed native worker did not unload')
        next_child, next_root = f.provider.gate(), f.provider.gate()
        f.provider.child_gates.append(next_child)
        f.provider.root_steps.extend([(function('followup_task', {'target': 'held', 'message': 'new exact turn'},
                                               'reuse-held', 'collaboration'), None),
                                      (message('reused worker parked'), next_root)])
        new_origin = f.start('rehydrate the existing worker')
        wait(lambda: sum(r['child'] for r in f.provider.requests) == 2, 'native child did not rehydrate')
        current = wait(lambda: (rows := f.rpc.call('thread/turns/list',
            {'threadId': child, 'limit': 1})['data']) and rows[0]['id'] != watched and rows[0],
            'native reused worker did not get a distinct turn')
        registered = cli('park', '--worker', '/root/held', '--reason', 'new native worker turn')
        assert registered['state'] == 'armed', registered
        assert len(f.state()['jobs']) == 2, f.state()
        second = observe()
        wait(lambda: all(j['observer_run'] for j in f.state()['jobs']), 'Rust did not claim reused worker turn')
        next_root.set()
        assert f.terminal(new_origin)['status'] == 'completed'
        next_child.set()
        assert f.terminal(current['id'], child)['status'] == 'completed'
        wait(lambda: delivered(2), 'reused native worker callbacks were not delivered once each')
        second.wait(timeout=6)
        state = f.state()
        assert {j['turn'] for j in state['jobs']} == {watched, current['id']}, state
        assert all(j['status'] == 'completed' for j in state['jobs']), state
        assert len({j['callback'] for j in state['jobs']}) == 2, state
        cli('observe')
        assert delivered(2), 'completed Rust restart duplicated native worker callback'
        f.report.update(runtime=str(runtime), scenario='worker-reuse', old_turn=watched,
                        reused_turn=current['id'], child=child, final_runtime_state=state)
        passed = True
    finally:
        for process, log in observers:
            stop(process)
            log.close()
        stop_rust_observers(f.home)
        lock.close()
        f.close(passed)
    print(json.dumps({'passed': passed, 'scenario': 'worker-reuse', 'remote_model_requests': 0}))


def run(runtime, binary, evidence, scenario):
    evidence.mkdir(parents=True, exist_ok=True)
    if scenario == 'worker-reuse':
        return run_worker(runtime, binary, evidence)
    f = PrivateServer(binary, evidence)
    observers = []
    passed = False
    gate = f.provider.gate()
    staged = f.home / 'runtime-package/bin/codex-plus-runtime'
    staged.parent.mkdir(parents=True)
    shutil.copy2(runtime, staged)
    lock = (f.home / 'codex-plus' / f'{f.parent}.supervisor.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    f.report.update(runtime=str(runtime), runtime_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),
                    scenario=scenario)

    def cli(*args):
        result = subprocess.run([str(staged), *args, '--parent', f.parent], env=f.env,
                                capture_output=True, text=True, timeout=12)
        f.report.setdefault('runtime_cli', []).append(dict(args=args, returncode=result.returncode,
                                                          stdout=result.stdout, stderr=result.stderr))
        assert result.returncode == 0, (args, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def observer():
        output = (evidence / f'observer-{len(observers)}.log').open('w')
        process = subprocess.Popen([str(staged), 'observe', '--parent', f.parent], env=f.env,
                                   stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
        observers.append((process, output))
        return process

    def delivered():
        state = f.state()
        return state['callbacks'] and all(c['state'] == 'delivered' for c in state['callbacks'])

    try:
        shell = (f'printf "run\\n" >> {shlex.quote(str(f.runs))}; '
                 f'i=0; while [ ! -f {shlex.quote(str(f.release))} ] && [ "$i" -lt 600 ]; '
                 'do sleep .1; i=$((i+1)); done; '
                 f'test -f {shlex.quote(str(f.release))} || exit 124; printf "native-lifecycle-complete\\n"')
        command = '# codex-plus-shell: auto\n' + shlex.join([str(staged), 'run', '--', '/bin/sh', '-c', shell])
        arguments = dict(cmd=command, yield_time_ms=1000, max_output_tokens=100, login=False)
        f.provider.root_steps.extend([(execute('text(await tools.exec_command(' + json.dumps(arguments) + '));'), None),
                                      (message('parked'), gate)])
        origin = f.start()
        process_id = f.process()
        wait(lambda: f.provider.count == 2 and f.runs.exists(), 'held native command did not yield')
        registration = cli('park', '--process', process_id, '--reason', 'Rust stock lifecycle')
        assert registration['state'] == 'armed', registration
        first = observer()
        wait(lambda: f.state()['jobs'][0]['observer_run'], 'Rust observer did not claim live command')
        original_owner = f.state()['jobs'][0]['observer_run']
        f.report.update(origin=origin, process=process_id, registration=registration,
                        old_generation=f.rpc.generation)
        if scenario == 'origin-abort':
            f.rpc.call('turn/interrupt', {'threadId': f.parent, 'turnId': origin})
            assert f.terminal(origin)['status'] == 'interrupted'
            wait(lambda: f.state()['parks'][0]['state'] == 'cancelled', 'real abort did not cancel Rust park')
            state = f.state()
            assert state['parks'][0]['cancel_kind'] == 'confirmed_origin', state
            assert not state['callbacks'], state
            gate.set()
            f.release.touch()
            wait(lambda: not f.terminals(), 'released command remained running after origin abort')
            first.wait(timeout=6)
            assert first.returncode == 0
            assert not f.state()['callbacks'] and f.provider.count == 2, f.state()
        else:
            gate.set()
            assert f.terminal(origin)['status'] == 'completed'
            assert f.provider.count == 2
            if scenario in ('observer-completed', 'observer-running'):
                stop(first)
                if scenario == 'observer-completed':
                    f.release.touch()
                    wait(lambda: not f.terminals(), 'command did not complete while observer stopped')
                second = observer()
                wait(lambda: f.state()['jobs'][0]['observer_run'] != original_owner,
                     'replacement observer did not acquire abandoned ownership')
                if scenario == 'observer-running':
                    wait(lambda: not f.state()['jobs'][0]['recovery_pending'],
                         'replacement observer did not positively reconfirm native running command')
                    assert f.provider.count == 2, 'reconfirmation invoked the model'
                    f.release.touch()
            else:
                first.send_signal(signal.SIGSTOP)
                f.rpc.close()
                stop(f.native)
                f.native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'],
                    env=f.env, stdin=subprocess.DEVNULL, stdout=f.log, stderr=f.log, start_new_session=True)
                f.rpc = wait(f.connect, 'replacement official daemon did not accept RPC')
                assert f.rpc.generation != f.report['old_generation']
                f.report['new_generation'] = f.rpc.generation
                assert f.thread()['status']['type'] == 'notLoaded'
                first.send_signal(signal.SIGCONT)
            wait(delivered, 'Rust lifecycle recovery did not deliver exactly one callback', timeout=22)
            wait(lambda: f.thread()['status']['type'] == 'idle', 'completion callback did not finish')
            state = f.state()
            assert len(state['callbacks']) == 1 and len(state['jobs']) == 1, state
            expected_report = scenario != 'observer-running'
            assert ('REPORT ONLY' if expected_report else 'LIVE COMPLETION') in state['callbacks'][0]['message'], state
            assert state['jobs'][0]['process'] == process_id
            assert state['jobs'][0]['status'] in (
                ('interrupted', 'failed', 'lost') if scenario == 'daemon-restart' else ('completed',)), state
            assert f.provider.count == 3, f.provider.requests
            assert f.runs.read_text() == 'run\n', 'recovery replayed shell execution'
            cli('observe')
            time.sleep(.15)
            assert f.provider.count == 3 and len(f.state()['callbacks']) == 1, 'restart duplicated a callback'
        f.report['final_runtime_state'] = f.state()
        passed = True
    finally:
        gate.set()
        f.release.touch()
        for process, output in observers:
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                stop(process)
            output.close()
        stop_rust_observers(f.home)
        lock.close()
        f.close(passed)
    print(json.dumps({'passed': passed, 'scenario': scenario, 'remote_model_requests': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--stock-binary', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--scenario', choices=SCENARIOS + ('all',), default='all')
    args = parser.parse_args()
    for scenario in SCENARIOS if args.scenario == 'all' else (args.scenario,):
        run(args.runtime.resolve(strict=True), args.stock_binary.resolve(strict=True),
            args.evidence.resolve() / scenario, scenario)
