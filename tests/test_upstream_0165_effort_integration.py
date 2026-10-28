"""Stock 0.165 effort switching wire contract; no remote calls or cache-hit claim."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from fixtures.advisor_provider import AdvisorProvider
from fixtures.turn_completion import wait_for_turn_completion
from test_advisor_integration import official_stock_identity
from test_background_integration import RecordingClient, stop, wait


def run(binary, evidence):
    version, digest = official_stock_identity(binary)
    assert version == 'codex-cli 0.165.0', version
    evidence.mkdir(parents=True, exist_ok=True)
    report = dict(binary=str(binary), version=version, sha256=digest,
                  remote_provider_requests=0, cache_hit_claim=False, passed=False)
    provider = rpc = native = None
    root = None
    try:
        with tempfile.TemporaryDirectory(prefix='effort165-') as temporary:
            root = Path(temporary)
            home, workspace = root / 'home', root / 'workspace'
            home.mkdir(mode=0o700)
            workspace.mkdir()
            provider = AdvisorProvider('unused')
            provider.reset('text')
            catalog = home / 'models.json'
            catalog.write_text(json.dumps({'models': [dict(
                slug='gpt-6.1-sol', display_name='gpt-6.1-sol', description='loopback model',
                default_reasoning_level='low',
                supported_reasoning_levels=[{'effort': effort, 'description': effort}
                                            for effort in ('low', 'high')],
                shell_type='shell_command', visibility='list', minimal_client_version=[0, 1, 0],
                supported_in_api=True, priority=0, upgrade=None,
                model_messages={'instructions_template': 'Reply using the loopback fixture.',
                                'approvals': None, 'auto_review': None, 'permissions': None},
                support_verbosity=False, default_verbosity=None, apply_patch_tool_type=None,
                truncation_policy={'mode': 'bytes', 'limit': 10000},
                supports_image_detail_original=False, context_window=272000,
                max_context_window=272000, experimental_supported_tools=[],
                supports_reasoning_effort_updates=True)]}))
            # OpenAI identity and supporting model metadata are both required by the upstream gate.
            (home / 'config.toml').write_text(f'''model="gpt-6.1-sol"
model_catalog_json={json.dumps(str(catalog))}
model_provider="isolated"
model_reasoning_effort="low"
analytics.enabled=false
features.daemon_auto_start=false
features.plugins=false
[model_providers.isolated]
name="OpenAI"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
supports_websockets=false
request_max_retries=0
stream_max_retries=0
''')
            env = {key: value for key, value in os.environ.items()
                   if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
            env.update(CODEX_HOME=str(home), HOME=str(root), RUST_LOG='warn')
            env.pop('CODEX_THREAD_ID', None)
            with (evidence / 'native.stderr').open('w') as native_log:
                try:
                    native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'],
                        env=env, stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log,
                        start_new_session=True)
                    socket = home / 'app-server-control/app-server-control.sock'
                    wait(socket.exists, 'private effort app-server did not start')
                    rpc = RecordingClient(socket)
                    parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                        'approvalPolicy': 'never', 'sandbox': 'read-only'})['thread']['id']
                    selected = ('low', 'high', 'high', 'low')
                    for number, effort in enumerate(selected, 1):
                        ident = rpc.call('turn/start', {'threadId': parent, 'effort': effort, 'input': [
                            {'type': 'text', 'text': f'Fixture turn {number}.', 'text_elements': []}]})['turn']['id']
                        wait_for_turn_completion(rpc, parent, ident)
                        wait(lambda: len((thread := rpc.call('thread/read', {
                            'threadId': parent, 'includeTurns': True})['thread'])['turns']) == number
                            and thread['turns'][-1]['id'] == ident
                            and thread['turns'][-1]['status'] == 'completed', 'effort turn did not complete')
                    bodies = [request['body'] for request in provider.requests]
                    assert len(bodies) == 4, bodies
                    assert all(body['model'] == 'gpt-6.1-sol' for body in bodies), bodies
                    assert [body['reasoning']['effort'] for body in bodies] == ['low'] * 4, bodies
                    updates = [[item for item in body['input'] if item.get('type') == 'configuration_update']
                               for body in bodies]
                    update = lambda effort: {'type': 'configuration_update', 'reasoning': {'effort': effort}}
                    low, high = update('low'), update('high')
                    assert updates == [[low], [low, high], [low, high], [low, high, low]], updates
                    keys = [body['prompt_cache_key'] for body in bodies]
                    assert keys[0] and keys == [keys[0]] * 4, keys
                    assert not rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']
                    assert not rpc.call('thread/queue/list', {'threadId': parent})['data']
                    report.update(passed=True, parent=parent, selected_efforts=selected,
                                  request_efforts=['low'] * 4, configuration_updates=updates,
                                  stable_prompt_cache_key=keys[0], loopback_provider_requests=len(bodies))
                finally:
                    if rpc:
                        rpc.close()
                    if native:
                        stop(native)
                    for path in home.glob('sessions/**/*.jsonl'):
                        shutil.copyfile(path, evidence / path.name)
                    (evidence / 'provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
                    (evidence / 'rpc-notifications.json').write_text(json.dumps(rpc.messages if rpc else [], indent=2) + '\n')
    finally:
        if provider:
            provider.close()
        report['cleanup'] = dict(private_root=str(root), private_root_removed=root is None or not root.exists(),
            native_returncode=native.poll() if native else None,
            provider_closed=provider is None or (provider.server.fileno() == -1 and not provider.thread.is_alive()))
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    assert report['cleanup']['private_root_removed'] and report['cleanup']['provider_closed'], report
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.evidence.resolve())
