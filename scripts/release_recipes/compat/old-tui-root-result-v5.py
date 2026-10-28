#!/usr/bin/env python3
"""Prepared actual previous TUI -> candidate wrapper -> Claude stub -> root tool-result gate."""
from recipe_context import CONFIG, DATA, OLD_VERSION, NEW_VERSION, UPSTREAM, source_identity
import argparse
import hashlib
import importlib.util
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
import traceback

sys.dont_write_bytecode = True
AREA = Path(__file__).resolve().parent
FROZEN = CONFIG['source_commit']
SUMMARY = 'OLD_TUI_ROOT_RECEIVED_CLAUDE_RESULT_candidate'
RESULT = dict(verdict='revise', summary=SUMMARY,
              findings=[dict(severity='low', message='Private transport fixture only.')],
              next_steps=['Restart with the new context attachment workflow.'])


def seed_finished(current, status, seed_turn, requests):
    assert requests and requests[0].get('seed_placeholder') is True
    assert len(requests) <= 2, 'seed made more than one actual provider request'
    assert all(not row.get('seed_placeholder') for row in requests[1:])
    if not current or current.get('id') != seed_turn:
        return False
    assert status not in ('failed', 'interrupted'), current
    return status == 'completed' and len(requests) == 2


def validate_root_result(result):
    assert result['state'] == 'finished' and result['exit_code'] == 0, result
    assert 'session_id' not in result and 'Script running with cell ID' not in result['output'], result
    values = [json.loads(line) for line in result['output'].splitlines() if line.startswith('{')]
    assert len(values) == 1, values
    value = values[0]
    assert {key: value[key] for key in RESULT} == RESULT, value
    assert value['model'] == 'claude-opus-5-5' and value['effort'] == 'xhigh', value
    assert value['log'] and result['log'], result
    return value


def self_check():
    value = RESULT | dict(model='claude-opus-5-5', effort='xhigh', log='/private/advisor.log')
    good = dict(state='finished', exit_code=0, output=json.dumps(value), log='/private/shell.log')
    assert validate_root_result(good) == value
    mutations = [good | dict(exit_code=1), good | dict(state='budget_expired'),
                 good | dict(session_id=1), good | dict(output=json.dumps(value | dict(model='gpt-6-astra'))),
                 good | dict(output=json.dumps(value | dict(summary='wrong result')))]
    for invalid in mutations:
        try:
            validate_root_result(invalid)
        except AssertionError:
            pass
        else:
            raise AssertionError('root-result validator accepted wrong evidence')
    print(json.dumps(dict(passed=True, valid_results=1, rejected_mutations=len(mutations),
                          native_execution='not run')))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args):
    source = args.source.resolve(strict=True)
    sys.path.insert(0, str(source / 'tests'))
    from fixtures.foreground_advisor_provider import ForegroundAdvisorProvider
    from fixtures.worker_shell_wait_provider import cell_result, wait_script
    from native_smoke import Terminal, advisor_rows, advisor_transcript_rows, composer_ready, wait, write_model_catalog
    from test_background_integration import RecordingClient, stop, wait as wait_server
    gate = load('mix_gate', AREA / 'verify-mixes.py')
    gate.source_identity(source, FROZEN)
    old, new = args.old_package.resolve(strict=True), args.new_package.resolve(strict=True)
    architecture = __import__('platform').machine()
    pin = gate.OLD_IDENTITIES[OLD_VERSION]
    identities = dict(old=gate.package_identity(old, OLD_VERSION, pin['commit'], pin['manifests'][architecture], role='old'),
                      new=gate.package_identity(new, NEW_VERSION, FROZEN, args.new_manifest_sha256))
    gate.distinct_packages(identities)
    stock = args.stock_binary.resolve(strict=True)
    gate.native_elf(stock)
    assert gate.digest(stock) == gate.STOCK_SHAS[architecture]
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = dict(passed=False, source_commit=FROZEN, packages=identities,
                  stock_binary=str(stock), stock_sha256=gate.digest(stock),
                  remote_provider_requests=0, consultation='explicit local stub; no SSH',
                  limitation='Private stub transport only; no external provider calls.')
    def timeout(signum, frame):
        raise TimeoutError('old TUI result proof exceeded 180 seconds')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(180)
    terminal = rpc = daemon = provider = None
    with tempfile.TemporaryDirectory(prefix='a45-') as temporary:
        root = Path(temporary)
        home, workspace, stubbin = root / 'h', root / 'w', root / 'b'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        stubbin.mkdir()
        context = workspace / 'explicit context file.txt'
        context.write_text('EXPLICIT_CONTEXT_candidate_ONLY\n')
        (workspace / 'omitted.txt').write_text('MUST_NOT_REACH_CLAUDE_candidate')
        os.environ['CODEX_PLUS_TEST_RUNTIME'] = str(new / 'bin/codex-plus-runtime')
        contract = load('claude_stub_contract', source / 'tests/test_claude_advisor.py')
        stub = contract.FAKE_CLAUDE
        marker = 'mode = (root / "mode").read_text()'
        assert stub.count(marker) == 1
        stub = stub.replace(marker, '(root / "stub.pid").write_text(str(os.getpid()))\ndeadline = time.monotonic() + 75\nwhile not (root / "release").exists():\n    assert time.monotonic() < deadline\n    time.sleep(.025)\n' + marker)
        (stubbin / 'claude').write_text(stub)
        (stubbin / 'claude').chmod(0o700)
        (root / 'mode').write_text('success')
        (root / 'result.json').write_text(json.dumps(RESULT))
        launcher = new / 'bin/codex-plus'
        command = '# codex-plus-shell: foreground\n# codex-plus-advisor: mixed version result proof\n' + shlex.join([
            str(launcher), 'run', '--', str(launcher), 'advisor', '--brief',
            'Read only the explicitly attached fixture evidence and return the structured fixture verdict.',
            '--host', 'local', '--context-file', str(context)])
        script = wait_script(command, budget_ms=90000, outer_ms=95000)
        (evidence / 'root-cell.js').write_text(script)
        provider = ForegroundAdvisorProvider(script)
        (home / 'config.toml').write_text(write_model_catalog(home, astra=True) + f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
suppress_unstable_features_warning=true
check_for_update_on_startup=false
analytics.enabled=false
features.daemon_auto_start=false
features.unified_exec=true
features.shell_tool=true
features.plugins=false
features.code_mode=true
features.code_mode_host=true
tui.status_line=["thread-name"]
[projects."{workspace}"]
trust_level="trusted"
[model_providers.isolated]
name="loopback old TUI new helper result proof"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
        env = {key: value for key, value in os.environ.items()
               if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        env.update(CODEX_HOME=str(home), HOME=str(root), PATH=str(stubbin)+os.pathsep+os.environ['PATH'],
                   CLAUDE_FIXTURE=str(root), RUST_LOG='warn')
        env.pop('CODEX_THREAD_ID', None)
        native_log = (evidence / 'official-daemon.stderr').open('w')
        try:
            daemon = subprocess.Popen([str(stock), 'app-server', '--listen', 'unix://'], env=env,
                stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log, start_new_session=True)
            socket = home / 'app-server-control/app-server-control.sock'
            wait_server(socket.exists, 'private official daemon did not start')
            rpc = RecordingClient(socket)
            parent = rpc.call('thread/start', dict(model='gpt-6.1-sol', cwd=str(workspace),
                approvalPolicy='never', sandbox='danger-full-access'))['thread']['id']
            # Save a genuine official root rollout before the old TUI resumes it.
            with provider.lock:
                provider.requests.append(dict(seed_placeholder=True))
            seed_turn = rpc.call('turn/start', dict(threadId=parent,
                input=[dict(type='text', text='Seed private root.', text_elements=[])]))['turn']['id']
            report['seed_turn'] = seed_turn
            def completed_seed():
                # Pump read-only RPC; only the exact canonical notification is terminal.
                rpc.call('thread/read', dict(threadId=parent, includeTurns=False))
                current = next((message['params']['turn'] for message in rpc.messages
                    if message.get('method') == 'turn/completed'
                    and message['params']['threadId'] == parent
                    and message['params']['turn']['id'] == seed_turn), None)
                status = current['status'] if current else None
                with provider.lock:
                    return seed_finished(current, status, seed_turn, provider.requests)
            wait_server(completed_seed, 'exact private seed turn and one provider request did not complete')
            with provider.lock:
                seed_requests = provider.requests[1:]
                assert len(seed_requests) == 1
                provider.requests.clear()
            fixture_name = 'old-previous-new-candidate-private-proof'
            rpc.call('thread/name/set', dict(threadId=parent, name=fixture_name))
            assert rpc.call('thread/read', dict(threadId=parent, includeTurns=False))['thread']['name'] == fixture_name
            assert provider.count == 0, 'manual naming unexpectedly requested a model'
            report['fixture_thread_name'] = fixture_name
            previous = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update(env)
                terminal = Terminal(old / 'bin/codex', home, workspace, parent=parent)
            finally:
                os.environ.clear()
                os.environ.update(previous)
            wait([terminal], lambda: composer_ready(terminal.text), 'old TUI did not resume actual root', 30)
            prompt = b'Run the explicit-context mixed-version transport fixture.'
            os.write(terminal.master, prompt)
            wait([terminal], lambda: prompt.decode() in terminal.text, 'fixture prompt did not render')
            os.write(terminal.master, b'\r')
            wait([terminal], lambda: (root / 'invoked.json').exists(), 'new helper did not invoke local Claude stub', 30)
            status_path = home / 'codex-plus' / f'{parent}.advisor.json'
            status = json.loads(status_path.read_text())
            assert status['parent'] == parent and status['model'] == 'claude-opus-5-5'
            assert status['state'] == 'running' and status['calls'] == 1 and status['effort'] == 'xhigh'
            wait([terminal], lambda: any('Opus 5.5 (xhigh)' in row and 'reviewing' in row for row in advisor_rows(terminal.text)),
                 'Previous frontend did not show the new helper Claude status', 10)
            assert any('Advisor on mixed version result proof' in row for row in advisor_transcript_rows(terminal.text)), terminal.text
            assert provider.count == 1, provider.requests
            (evidence / 'running-screen.txt').write_text(terminal.text)
            (root / 'release').touch()
            wait([terminal], lambda: provider.count == 2 and 'Reviewed advisor result.' in terminal.text,
                 'root did not receive tool result and finish its review', 30)
            received = cell_result(provider.requests[1]['body'])
            value = validate_root_result(received)
            assert Path(received['log']).is_file() and Path(value['log']).is_file()
            invoked = json.loads((root / 'invoked.json').read_text())
            assert 'EXPLICIT_CONTEXT_candidate_ONLY' in invoked['prompt']
            assert 'MUST_NOT_REACH_CLAUDE_candidate' not in invoked['prompt']
            assert invoked['argv'][invoked['argv'].index('--model')+1] == 'claude-opus-5-5'
            assert invoked['argv'][invoked['argv'].index('--effort')+1] == 'xhigh'
            final = json.loads(status_path.read_text())
            assert final['state'] == 'done' and final['calls'] == 1
            assert any('Advised on mixed version result proof' in row for row in advisor_transcript_rows(terminal.text))
            assert provider.count == 2
            classification = []
            for request in provider.requests:
                body = request['body']
                metadata = body['client_metadata']
                structured = json.loads(metadata['x-codex-turn-metadata'])
                assert metadata['thread_id'] == parent and structured.get('thread_source') != 'thread_title'
                classification.append(dict(thread_id=metadata['thread_id'], turn_id=metadata['turn_id'],
                    thread_source=structured.get('thread_source'), tools_count=len(body.get('tools', []))))
            assert len({row['turn_id'] for row in classification}) == 1
            report.update(root_request_classification=classification, title_request_count=0)
            report.update(passed=True, parent=parent, root_requests=2, seed_requests=seed_requests,
                          root_tool_result=received, advisor_result=value, final_status=final,
                          old_claude_display_observed=True, explicit_context_only=True,
                          caller_restart_required_before_production_claude=False)
        except BaseException:
            report['error'] = traceback.format_exc()
            raise
        finally:
            signal.alarm(0)
            (root / 'release').touch()
            if terminal:
                (evidence / 'terminal.ansi').write_bytes(terminal.output)
                (evidence / 'complete-screen.txt').write_text(terminal.text)
                terminal.close()
            if rpc:
                (evidence / 'official-rpc.json').write_text(json.dumps(rpc.messages, indent=2))
                rpc.close()
            if daemon:
                stop(daemon)
            native_log.close()
            for path in (home / 'codex-plus/logs').glob('*.log'):
                shutil.copyfile(path, evidence / path.name)
            for path in home.glob('sessions/**/*.jsonl'):
                shutil.copyfile(path, evidence / path.name)
            if provider:
                (evidence / 'root-provider-requests.json').write_text(json.dumps(provider.requests, indent=2))
                provider.close()
            deadline = time.monotonic() + 8
            while gate.owned_processes(home) and time.monotonic() < deadline:
                time.sleep(.05)
            survivors = gate.owned_processes(home)
            report['owned_survivors_after_cleanup'] = survivors
            if survivors:
                report['passed'] = False
                report['cleanup_error'] = 'private CODEX_HOME processes survived cleanup'
            report['root_provider_requests_observed'] = provider.count if provider else 0
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            assert not survivors, survivors
    print(json.dumps(dict(passed=report['passed'], evidence=str(evidence / 'report.json'))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-check', action='store_true')
    for name in ('source', 'old-package', 'new-package', 'stock-binary', 'evidence'):
        parser.add_argument('--'+name, type=Path)
    parser.add_argument('--new-manifest-sha256')
    args = parser.parse_args()
    if args.self_check:
        self_check()
    else:
        assert all(getattr(args, name) for name in ('source', 'old_package', 'new_package',
            'stock_binary', 'evidence', 'new_manifest_sha256')), 'all final package inputs are required'
        run(args)
