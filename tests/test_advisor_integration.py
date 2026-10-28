"""Official stock exec acceptance in private homes with loopback-only providers.

Run: python3 tests/test_advisor_integration.py OFFICIAL_CODEX --evidence DIRECTORY
"""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from fixtures.advisor_provider import AdvisorProvider, RESULT
from fixtures.native_server import AppServer
from test_background_integration import REPO, stop, wait


PARENT = '00000000-0000-0000-0000-000000000001'
OFFICIAL_SHA256 = {
    'x86_64': '399ff69c431c066e40de318fba45c2b07f45a76b19939d247ee4828fc70c4215',
    'aarch64': '8a96f66d059d653e68bfd5f3c79337e6f1d7d170d80bf705eb7d0249b34c0ece',
}.get(platform.machine())
OFFICIAL_165_SHA256 = {
    'x86_64': 'c06a67fd8ac76508dc78bd99ce20c9f317b8d568beda59485f3f4134df9962d5',
    'aarch64': 'b32ece31af06440da980b733fb8fefdd1f193439595fda84096f1caaceec543f',
}.get(platform.machine())
MODEL = {'id': 'catalog-astra-id', 'model': 'gpt-6-astra', 'hidden': False,
         'supportedReasoningEfforts': [{'reasoningEffort': 'xhigh', 'description': 'Extra high'}]}


def official_stock_identity(binary):
    version = subprocess.check_output([str(binary), '--version'], text=True).strip()
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    reviewed = {'codex-cli 0.163.0': OFFICIAL_SHA256, 'codex-cli 0.165.0': OFFICIAL_165_SHA256}
    assert version in reviewed, f'expected a reviewed official stock version, got {version}'
    assert reviewed[version] is not None, f'unsupported stock fixture architecture: {platform.machine()}'
    assert digest == reviewed[version], f'expected pinned official stock binary for {version}, got {digest}'
    return version, digest


def advertised_tool_names(value, inside_tools=False):
    """Read tool descriptors, including Responses tool bundles; ignore prose."""
    names = set()
    if isinstance(value, dict):
        if inside_tools and isinstance(value.get('name'), str):
            names.add(value['name'])
        for key, child in value.items():
            if key != 'description':
                names.update(advertised_tool_names(child, inside_tools or key in (
                    'tools', 'additional_tools', 'additionalTools',
                    'input_additional_tools', 'inputAdditionalTools')))
    elif isinstance(value, list):
        for child in value:
            names.update(advertised_tool_names(child, inside_tools))
    return names


def assert_no_delegation_tools(body):
    names = advertised_tool_names(body)
    assert names, 'provider request has no recognized tool descriptors'
    forbidden = {name for name in names if any(word in name.lower() for word in (
        'collaboration', 'delegat', 'spawn', 'followup_task', 'send_message',
        'interrupt_agent', 'list_agents', 'wait_agent', 'close_agent', 'resume_agent', 'send_input'))}
    assert not forbidden, f'advisor advertises delegation tools: {sorted(forbidden)}'
    return names


def run(binary, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'report.json').unlink(missing_ok=True)
    captures = evidence / 'exec-arguments.jsonl'
    captures.unlink(missing_ok=True)
    version, digest = official_stock_identity(binary)
    report = {'binary': str(binary), 'version': version, 'sha256': digest, 'remote_provider_requests': 0, 'passed': []}
    with tempfile.TemporaryDirectory(prefix='advisor-stock-') as temporary:
        root = Path(temporary)
        home, workspace, package = root / 'home', root / 'workspace', root / 'package with spaces'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        (workspace / 'evidence.txt').write_text('readable-advisor-evidence\n')
        (package / 'bin').mkdir(parents=True)
        shutil.copyfile(REPO / 'bin/codex-plus', package / 'bin/codex-plus')
        (package / 'bin/codex-plus').chmod(0o755)
        shutil.copytree(REPO / 'codex_plus', package / 'codex_plus',
                        ignore=shutil.ignore_patterns('__pycache__'))
        socket = home / 'app-server-control/app-server-control.sock'
        socket.parent.mkdir()
        rpc = AppServer(PARENT, workspace, 0, models=[MODEL], socket_path=socket)
        forbidden = workspace / 'must-not-exist'
        provider = AdvisorProvider('cat evidence.txt; printf forbidden > must-not-exist')
        wrapper = package / 'bin/codex'
        config = [
            '-c', 'model_provider="isolated"',
            '-c', 'model_providers.isolated.name="advisor loopback acceptance"',
            '-c', f'model_providers.isolated.base_url={json.dumps(provider.url)}',
            '-c', 'model_providers.isolated.wire_api="responses"',
            '-c', 'model_providers.isolated.requires_openai_auth=false',
            '-c', 'model_providers.isolated.request_max_retries=0',
            '-c', 'model_providers.isolated.stream_max_retries=0',
            '-c', 'features.unified_exec=true', '-c', 'features.shell_tool=true',
            '-c', 'features.plugins=false', '-c', 'features.daemon_auto_start=false',
        ]
        wrapper.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                           f'with open({str(captures)!r}, "a") as stream: '
                           'stream.write(json.dumps(sys.argv[1:])+"\\n")\n'
                           f'os.execv({str(binary)!r}, [{str(binary)!r}, *sys.argv[1:-1], '
                           f'*{config!r}, sys.argv[-1]])\n')
        wrapper.chmod(0o755)
        # Deliberately hostile config proves the production ignore-user-config flag survives.
        (home / 'config.toml').write_text('model="must-not-be-used"\nmodel_provider="missing"\n')
        env = {key: value for key, value in os.environ.items()
               if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        env.update(CODEX_HOME=str(home), CODEX_THREAD_ID=PARENT, HOME=str(root), RUST_LOG='warn')
        command = [str(package / 'bin/codex-plus'), 'advisor', '--provider', 'astra', '--brief',
                   'Inspect evidence.txt and check whether repository writes are prohibited.']

        def status():
            path = home / 'codex-plus' / f'{PARENT}.advisor.json'
            return json.loads(path.read_text())

        def launch(mode):
            provider.reset(mode)
            result = subprocess.run(command, cwd=workspace, env=env, capture_output=True,
                                    text=True, timeout=35)
            (evidence / f'{mode}.stdout').write_text(result.stdout)
            (evidence / f'{mode}.stderr').write_text(result.stderr)
            return result

        try:
            result = launch('success')
            assert result.returncode == 0, result.stderr
            value = json.loads(result.stdout)
            assert {key: value[key] for key in RESULT} == RESULT, value
            assert value['model'] == 'gpt-6-astra' and value['effort'] == 'xhigh', value
            assert not forbidden.exists(), 'stock read-only sandbox allowed a workspace write'
            assert len(provider.requests) == 2, provider.requests
            outputs = [item for item in provider.requests[1]['body']['input']
                       if item.get('type') == 'function_call_output']
            assert len(outputs) == 1, outputs
            assert 'readable-advisor-evidence' in str(outputs), outputs
            assert any(text in str(outputs).lower() for text in ('permission denied', 'read-only file system')), outputs
            assert status()['state'] == 'done' and status()['calls'] == 1, status()
            report['passed'].append('official stock exec reads evidence and kernel sandbox denies workspace write')
            for request in provider.requests:
                body = request['body']
                assert body['model'] == 'gpt-6-astra', body
                assert body['reasoning']['effort'] == 'xhigh', body
            report['passed'].append('selected exact Astra model and xhigh reach Responses requests')
            for mode, error in (('invalid', 'invalid_result'), ('failure', 'exec_failed')):
                result = launch(mode)
                assert result.returncode != 0 and not result.stdout.strip(), result
                assert status()['state'] == 'error' and status()['error'] == error, status()
                report['passed'].append(f'{mode} response fails visibly without approval')
            provider.reset('held')
            process = subprocess.Popen(command, cwd=workspace, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                wait(provider.entered.is_set, 'advisor never entered held provider request')
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=8)
                (evidence / 'interrupted.stdout').write_text(stdout)
                (evidence / 'interrupted.stderr').write_text(stderr)
                assert process.returncode != 0 and not stdout.strip(), (stdout, stderr)
                assert status()['state'] == 'error' and status()['error'] == 'interrupted', status()
            finally:
                stop(process)
                provider.release.set()
            report['passed'].append('SIGTERM interrupts owned stock exec and publishes an error')
            tool_names = set()
            for request in provider.requests:
                tool_names.update(assert_no_delegation_tools(request['body']))
            report['advertised_tool_names'] = sorted(tool_names)
            report['passed'].append('actual provider tool descriptors contain no delegation, spawn, or collaboration tools')
            arguments = [json.loads(line) for line in captures.read_text().splitlines()]
            for args in arguments:
                assert args[:9] == ['exec', '--ignore-user-config', '--ignore-rules', '--sandbox',
                                    'read-only', '--skip-git-repo-check', '--ephemeral', '-m', 'gpt-6-astra'], args
                assert args[9:15] == ['-c', 'model_reasoning_effort="xhigh"',
                                      '-c', 'agents.enabled=false', '-c', 'features.multi_agent_v2=false'], args
                assert args[15] == '--output-schema' and args[17] == '--output-last-message' and args[-1] == '-', args
            assert len(arguments) == 4, arguments
            assert all(request['method'] in ('initialize', 'initialized', 'thread/read', 'model/list')
                       for request in rpc.requests), rpc.requests
            assert all(not request.get('params', {}).get('includeTurns') for request in rpc.requests), rpc.requests
            assert not rpc.errors, rpc.errors
            report['passed'].append('immutable production args, isolated config, root-only metadata, no transcript or daemon mutation')
            (evidence / 'final-status.json').write_text(json.dumps(status(), indent=2) + '\n')
        finally:
            for path in (home / 'codex-plus/logs').glob('*.log'):
                shutil.copyfile(path, evidence / path.name)
            (evidence / 'provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            (evidence / 'rpc-requests.json').write_text(json.dumps(rpc.requests, indent=2) + '\n')
            provider.close()
            rpc.close()
    (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, default=Path('/tmp/codex-plus-advisor-stock-evidence'))
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve())
