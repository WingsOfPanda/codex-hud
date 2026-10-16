"""Stock daemon acceptance: native children wait without model polling.

Run: python3 tests/test_worker_shell_wait_integration.py /path/to/official/codex
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time

from fixtures.worker_shell_wait_provider import WorkerShellWaitProvider, cell_result
from test_background_integration import REPO, RecordingClient, stop, wait


def run(binary, evidence, scenario='success', seconds=65):
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'binary': str(binary), 'scenario': scenario, 'remote_model_requests': 0}
    rpc = native = child = None
    with tempfile.TemporaryDirectory(prefix='w-') as temporary:
        root = Path(temporary)
        home, workspace = root / 'home', root / 'workspace'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        done = workspace / 'done'
        duration = seconds if scenario == 'success' else 8 if scenario == 'budget' else 3
        exit_code = 7 if scenario == 'failure' else 0
        command = ('# codex-plus-shell: background\n' + shlex.join([
            str(REPO / 'bin/codex-plus'), 'run', '--', 'bash', '-c',
            f'sleep {duration}; echo worker-shell-result; touch {shlex.quote(str(done))}; exit {exit_code}']))
        provider = WorkerShellWaitProvider(command, budget_ms=1000 if scenario == 'budget' else 110000)
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
features.code_mode=true
[model_providers.isolated]
name="loopback worker shell wait acceptance"
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
            wait(lambda: socket_path.exists(), 'isolated server did not start')
            rpc = RecordingClient(socket_path)
            parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                              'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
            rpc.call('turn/start', {'threadId': parent, 'input': [{'type': 'text',
                     'text': 'Spawn a worker to wait for its finite command.', 'text_elements': []}]})
            wait(lambda: provider.child_requests == 1, 'native child did not request its shell tool')

            def child_thread():
                rpc.call('thread/read', {'threadId': parent})
                return next((message['params']['item']['agentThreadId'] for message in rpc.messages
                    if message.get('method') == 'item/completed'
                    and message.get('params', {}).get('item', {}).get('type') == 'subAgentActivity'
                    and message['params']['item']['kind'] == 'started'), None)

            child = wait(child_thread, 'native child thread not found')
            wait(lambda: rpc.call('thread/backgroundTerminals/list', {'threadId': child})['data'],
                 'child command did not yield a native terminal')
            started = time.monotonic()
            deadline = started + duration + 30
            observations = []
            while not done.exists() and provider.child_requests == 1:
                assert time.monotonic() < deadline, 'child shell wait exceeded its fixture deadline'
                thread = rpc.call('thread/read', {'threadId': child, 'includeTurns': True})['thread']
                queue = rpc.call('thread/queue/list', {'threadId': parent})['data']
                assert thread['turns'][-1]['status'] == 'inProgress', thread
                assert not queue, queue
                assert not completed_activity(rpc.messages), rpc.messages[-10:]
                assert provider.root_requests == 2, provider.requests
                observations.append({'elapsed': time.monotonic() - started,
                                     'child_requests': provider.child_requests,
                                     'turn_status': thread['turns'][-1]['status']})
                if scenario == 'interrupt':
                    rpc.call('turn/interrupt', {'threadId': child, 'turnId': thread['turns'][-1]['id']})
                    break
                time.sleep(.25)
            if scenario == 'interrupt':
                wait(lambda: rpc.call('thread/read', {'threadId': child, 'includeTurns': True})[
                     'thread']['turns'][-1]['status'] == 'interrupted', 'child did not interrupt')
                assert provider.child_requests == 1, provider.requests
                assert not completed_activity(rpc.messages), rpc.messages[-10:]
                report['interrupted_without_success'] = True
            else:
                wait(lambda: provider.child_requests == 2, 'child did not return after command', timeout=15)
                thread = wait(lambda: (value := rpc.call('thread/read', {'threadId': child,
                    'includeTurns': True})['thread'])['turns'][-1]['status'] == 'completed' and value,
                    'child did not complete')
                if scenario != 'budget':
                    assert done.exists(), 'child returned before its command finished'
                result = cell_result(next(request['body'] for request in provider.requests
                                          if request['child'] and cell_result(request['body']) is not None))
                if scenario == 'budget':
                    assert result['state'] == 'budget_expired' and 'exit_code' not in result, result
                    assert 'session_id' in result and not done.exists(), result
                else:
                    assert result['state'] == 'finished' and result['exit_code'] == exit_code, result
                    assert 'session_id' not in result and 'worker-shell-result' in result['output'], result
                    assert Path(result['log']).is_file(), result
                    assert f'codex-plus result: exit={exit_code}' in result['output'], result
                    if scenario == 'success':
                        assert observations[-1]['elapsed'] >= seconds - 2, observations[-1]
                rpc.call('thread/read', {'threadId': parent})
                assert len(completed_activity(rpc.messages)) == 1, completed_activity(rpc.messages)
                assert provider.child_requests == 2 and provider.root_requests == 2, provider.requests
                rpc.call('turn/start', {'threadId': parent, 'input': [{'type': 'text',
                    'text': 'Report the received child result.', 'text_elements': []}]})
                wait(lambda: provider.root_requests == 4, 'parent did not receive child completion')
                parent_request = [request['body'] for request in provider.requests if not request['child']][-1]
                completions = [entry for entry in parent_request['input']
                    if entry.get('type') == 'agent_message' and entry.get('author') == '/root/held']
                assert len(completions) == 1, completions
                assert json.dumps(json.dumps(result))[1:-1] in json.dumps(completions), completions
                report.update(result=result, parent_completion=completions)
            report.update(passed=True, parent=parent, child=child, observations=observations,
                          child_requests=provider.child_requests, root_requests=provider.root_requests)
        finally:
            # Turn interruption can leave the shell alive; stop only fixture terminals.
            if rpc and child:
                terminals = rpc.call('thread/backgroundTerminals/list', {'threadId': child})['data']
                report['cleanup_terminated_processes'] = [terminal['processId'] for terminal in terminals]
                for terminal in terminals:
                    rpc.call('thread/backgroundTerminals/terminate',
                             {'threadId': child, 'processId': terminal['processId']})
                wait(lambda: not rpc.call('thread/backgroundTerminals/list', {'threadId': child})['data'],
                     'fixture terminal survived explicit cleanup')
            if rpc:
                rpc.close()
            if native:
                stop(native)
            native_log.close()
            for path in home.glob('sessions/**/*.jsonl'):
                shutil.copy2(path, evidence / path.name)
            for path in (home / 'codex-plus/logs').glob('*.log'):
                shutil.copy2(path, evidence / 'command.log')
            (evidence / 'provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            (evidence / 'rpc-notifications.json').write_text(json.dumps(rpc.messages if rpc else [], indent=2) + '\n')
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            provider.close()
    print(json.dumps({key: value for key, value in report.items() if key not in ('observations', 'parent_completion')}, indent=2))


def completed_activity(messages):
    return [message for message in messages if message.get('method') == 'item/completed'
            and message.get('params', {}).get('item', {}).get('type') == 'subAgentActivity'
            and message['params']['item'].get('kind') == 'completed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, default=REPO / '.build/worker-wait/acceptance')
    parser.add_argument('--scenario', choices=('success', 'failure', 'interrupt', 'budget'), default='success')
    parser.add_argument('--seconds', type=int, default=65)
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve(), args.scenario, args.seconds)
