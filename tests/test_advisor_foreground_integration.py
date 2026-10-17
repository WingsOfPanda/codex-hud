"""Official root consults a held advisor without model polling or a park.

Run: python3 tests/test_advisor_foreground_integration.py OFFICIAL_CODEX
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from fixtures.advisor_provider import AdvisorProvider, RESULT
from fixtures.foreground_advisor_provider import ForegroundAdvisorProvider
from fixtures.worker_shell_wait_provider import cell_result
from test_advisor_integration import OFFICIAL_SHA256
from test_background_integration import REPO, RecordingClient, stop, wait


def run(binary, evidence, seconds=65):
    evidence.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert digest == OFFICIAL_SHA256, f'expected pinned official stock binary, got {digest}'
    report = {'binary': str(binary), 'sha256': digest, 'remote_provider_requests': 0}
    rpc = native = None
    with tempfile.TemporaryDirectory(prefix='advisor-foreground-') as temporary:
        root = Path(temporary)
        home, workspace, package = root / 'home', root / 'workspace', root / 'package'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        (package / 'bin').mkdir(parents=True)
        shutil.copyfile(REPO / 'bin/codex-plus', package / 'bin/codex-plus')
        (package / 'bin/codex-plus').chmod(0o755)
        shutil.copytree(REPO / 'codex_plus', package / 'codex_plus',
                        ignore=shutil.ignore_patterns('__pycache__'))
        advisor = AdvisorProvider('unused', hold_timeout=seconds + 40)
        advisor.reset('held')
        config = ['-c', 'model_provider="isolated"',
                  '-c', 'model_providers.isolated.name="advisor foreground acceptance"',
                  '-c', f'model_providers.isolated.base_url={json.dumps(advisor.url)}',
                  '-c', 'model_providers.isolated.wire_api="responses"',
                  '-c', 'model_providers.isolated.requires_openai_auth=false',
                  '-c', 'model_providers.isolated.request_max_retries=0',
                  '-c', 'model_providers.isolated.stream_max_retries=0',
                  '-c', 'features.plugins=false', '-c', 'features.daemon_auto_start=false']
        wrapper = package / 'bin/codex'
        wrapper.write_text('#!' + sys.executable + '\nimport os,sys\n'
                           f'os.execv({str(binary)!r}, [{str(binary)!r}, *sys.argv[1:-1], '
                           f'*{config!r}, sys.argv[-1]])\n')
        wrapper.chmod(0o755)
        command = '# codex-plus-shell: foreground\n' + shlex.join([
            str(package / 'bin/codex-plus'), 'run', '--',
            str(package / 'bin/codex-plus'), 'advisor', '--brief',
            'Assess this decision before the root continues.'])
        script = (REPO / 'docs/advisor.md').read_text().split('```js\n', 1)[1].split('```', 1)[0]
        command_line = next(line for line in script.splitlines() if line.strip().startswith('cmd:'))
        script = script.replace(command_line, '  cmd: ' + json.dumps(command) + ',')
        outer_yield = json.loads(script.splitlines()[0].split('// @exec: ', 1)[1])['yield_time_ms']
        (evidence / 'documented-cell.js').write_text(script)
        provider = ForegroundAdvisorProvider(script)
        # Match the existing native smoke catalog without importing its PTY dependencies.
        catalog = home / 'models.json'
        catalog.write_text(json.dumps({'models': [dict(slug=slug, display_name=slug,
            description='loopback fixture model', default_reasoning_level='high',
            supported_reasoning_levels=[{'effort': effort, 'description': effort}
                                        for effort in ('high', 'xhigh')],
            shell_type='shell_command', visibility='list', minimal_client_version=[0, 1, 0],
            supported_in_api=True, priority=0, upgrade=None,
            model_messages={'instructions_template': 'Read fixture evidence.',
                            'approvals': None, 'auto_review': None, 'permissions': None},
            support_verbosity=False, default_verbosity=None, apply_patch_tool_type=None,
            truncation_policy={'mode': 'bytes', 'limit': 10000},
            supports_image_detail_original=False, context_window=272000,
            max_context_window=272000, experimental_supported_tools=[])
            for slug in ('gpt-6.1-sol', 'gpt-6-astra')]}))
        (home / 'config.toml').write_text(f'''model="gpt-6.1-sol"
model_catalog_json={json.dumps(str(catalog))}
model_provider="isolated"
model_reasoning_effort="high"
analytics.enabled=false
features.daemon_auto_start=false
features.unified_exec=true
features.shell_tool=true
features.plugins=false
features.code_mode=true
[model_providers.isolated]
name="loopback root foreground acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
        env = {key: value for key, value in os.environ.items()
               if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        env.update(CODEX_HOME=str(home), HOME=str(root), PYTHONPATH=str(REPO), RUST_LOG='warn')
        env.pop('CODEX_THREAD_ID', None)
        native_log = (evidence / 'native.stderr').open('w')
        try:
            native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
                                      stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log,
                                      start_new_session=True)
            socket = home / 'app-server-control/app-server-control.sock'
            wait(socket.exists, 'private app-server did not start')
            rpc = RecordingClient(socket)
            parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
            rpc.call('turn/start', {'threadId': parent, 'input': [{'type': 'text',
                'text': 'Consult the advisor in the foreground, then review its result.', 'text_elements': []}]})
            wait(advisor.entered.is_set, 'advisor did not enter held loopback request')
            started = time.monotonic()
            status_path = home / 'codex-plus' / f'{parent}.advisor.json'
            observations = []
            while time.monotonic() - started < seconds:
                thread = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
                status = json.loads(status_path.read_text())
                assert thread['turns'][-1]['status'] == 'inProgress', thread
                assert provider.count == 1, provider.requests
                assert status['state'] == 'running' and status['effort'] == 'xhigh', status
                assert status['model'] == 'gpt-6-astra' and status['calls'] == 1, status
                assert not rpc.call('thread/queue/list', {'threadId': parent})['data']
                assert not (home / 'codex-plus/background.sqlite').exists(), 'foreground consultation created a park ledger'
                observations.append({'elapsed': time.monotonic() - started,
                                     'root_requests': provider.count, 'status': status})
                time.sleep(.25)
            held_seconds = time.monotonic() - started
            advisor.release.set()
            wait(lambda: provider.count == 2, 'root did not receive advisor result', timeout=20)
            wait(lambda: rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})[
                'thread']['turns'][-1]['status'] == 'completed', 'root did not complete its review')
            result = cell_result(provider.requests[1]['body'])
            assert result['state'] == 'finished' and result['exit_code'] == 0, result
            assert 'session_id' not in result and 'Script running with cell ID' not in result['output'], result
            values = [json.loads(line) for line in result['output'].splitlines() if line.startswith('{')]
            assert len(values) == 1 and {key: values[0][key] for key in RESULT} == RESULT, values
            assert values[0]['model'] == 'gpt-6-astra' and values[0]['effort'] == 'xhigh', values
            assert Path(result['log']).is_file() and Path(values[0]['log']).is_file(), result
            final_status = json.loads(status_path.read_text())
            assert final_status['state'] == 'done' and final_status['calls'] == 1, final_status
            assert final_status['last_duration_secs'] >= seconds, final_status
            assert provider.count == 2 and len(advisor.requests) == 1
            assert not (home / 'codex-plus/background.sqlite').exists()
            assert not rpc.call('thread/queue/list', {'threadId': parent})['data']
            assert not rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']
            assert all(request['body']['model'] == 'gpt-6-astra' and
                       request['body']['reasoning']['effort'] == 'xhigh' for request in advisor.requests)
            report.update(passed=True, parent=parent, held_seconds=held_seconds,
                          outer_yield_ms=outer_yield, root_requests=provider.count,
                          advisor_requests=len(advisor.requests), parks=0, callbacks=0,
                          result=result, final_status=final_status, observations=observations)
        finally:
            advisor.release.set()
            if rpc and 'parent' in locals():
                for terminal in rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']:
                    rpc.call('thread/backgroundTerminals/terminate',
                             {'threadId': parent, 'processId': terminal['processId']})
            if rpc:
                rpc.close()
            if native:
                stop(native)
            native_log.close()
            for path in (home / 'codex-plus/logs').glob('*.log'):
                shutil.copyfile(path, evidence / path.name)
            for path in home.glob('sessions/**/*.jsonl'):
                shutil.copyfile(path, evidence / path.name)
            (evidence / 'root-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            (evidence / 'advisor-requests.json').write_text(json.dumps(advisor.requests, indent=2) + '\n')
            (evidence / 'rpc-notifications.json').write_text(json.dumps(rpc.messages if rpc else [], indent=2) + '\n')
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            provider.close()
            advisor.close()
    print(json.dumps({key: value for key, value in report.items() if key != 'observations'}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, default=Path('/tmp/codex-plus-advisor-foreground-evidence'))
    parser.add_argument('--seconds', type=int, default=65)
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve(), args.seconds)
