"""Private old-Plus-TUI transition across official 0.163/0.165 daemons."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback

from fixtures.upstream_0163 import BoundaryProvider, message
from native_smoke import Terminal, composer_ready, wait, write_model_catalog
from test_advisor_integration import official_stock_identity
from test_background_integration import RecordingClient, stop, wait as wait_server
from test_rust_runtime_stock_integration import stop_rust_observers

SEED = 'TRANSITION_SEED_IDLE'
RESPONSE = 'TRANSITION_CONTROLLED_RESPONSE'
SCENARIOS = ('open-idle-restart', 'fresh-old-tui-on-165')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_identity(package, manifest_sha256, tui_sha256):
    manifest_path = package / 'codex-plus.json'
    assert digest(manifest_path) == manifest_sha256, 'old package manifest does not match reviewed identity'
    manifest = json.loads(manifest_path.read_text())
    assert manifest['release'] in {'0.163.0-plus.0.4.8', '0.163.0-plus.0.4.9'}, manifest['release']
    for name in ('bin/codex', 'bin/codex-plus-runtime', 'bin/codex-code-mode-host'):
        assert digest(package / name) == manifest['files'][name]['sha256'], name
    assert digest(package / 'bin/codex') == tui_sha256, 'old TUI does not match reviewed identity'
    version = subprocess.check_output([str(package / 'bin/codex'), '--version'], text=True).strip()
    assert version == 'codex-cli 0.163.0', version
    return dict(path=str(package), release=manifest['release'], manifest_sha256=manifest_sha256,
                tui_sha256=tui_sha256, tui_version=version, plus_commit=manifest['plus_commit'],
                upstream=manifest['upstream'], adjacent_files={name: manifest['files'][name]['sha256']
                    for name in ('bin/codex-plus-runtime', 'bin/codex-code-mode-host')})


def owned_processes(home):
    processes = []
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            if f'CODEX_HOME={home}'.encode() in (entry / 'environ').read_bytes().split(b'\0'):
                processes.append(dict(pid=int(entry.name),
                    command=(entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')))
        except OSError:
            pass
    return processes


def draft_visible(terminal, text):
    return any(line.lstrip().startswith('›') and text in line for line in terminal.text.splitlines())


def run(package, old_stock, new_stock, evidence, scenario, identity):
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = dict(passed=False, scenario=scenario, identities=identity, remote_provider_requests=0,
                  forced_reconnect=False, managed_daemon=False, packaged_rollback_qualified=False)
    terminal = provider = rpc = None
    daemons, logs, notifications = [], [], []
    temporary_directory = tempfile.TemporaryDirectory(prefix='tui165-')
    with temporary_directory as temporary:
        root = Path(temporary)
        home, workspace = root / 'home', root / 'workspace'
        home.mkdir(mode=0o700)
        workspace.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not any(word in key.upper() for word in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        env.update(CODEX_HOME=str(home), HOME=str(root), RUST_LOG='warn')
        env.pop('CODEX_THREAD_ID', None)
        env.pop('CODEX_PLUS_TEST_RUNTIME', None)
        socket = home / 'app-server-control/app-server-control.sock'

        def launch(binary, label):
            log = (evidence / f'{label}.stderr').open('w')
            logs.append(log)
            daemon = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            daemons.append(daemon)

            def connect():
                assert daemon.poll() is None, f'{label} daemon exited; inspect its stderr'
                try:
                    return RecordingClient(socket)
                except (OSError, ValueError):
                    return None

            client = wait_server(connect, f'{label} daemon did not accept private RPC')
            assert not client.initialize_result.get('queueIdempotency'), 'official stock daemon required'
            return daemon, client

        def screen(label):
            (evidence / f'{label}-screen.txt').write_text(terminal.text + '\n')

        try:
            provider = BoundaryProvider()
            (home / 'config.toml').write_text(write_model_catalog(home) + f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
suppress_unstable_features_warning=true
check_for_update_on_startup=false
analytics.enabled=false
features.daemon_auto_start=false
features.plugins=false
tui.status_line=["thread-name"]
[projects."{workspace}"]
trust_level="trusted"
[model_providers.isolated]
name="loopback TUI transition diagnostic"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
            initial = old_stock if scenario == 'open-idle-restart' else new_stock
            daemon, rpc = launch(initial, 'initial-daemon')
            parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
            provider.parent = parent
            provider.root_steps.append((message(SEED), None))
            seed = rpc.call('turn/start', {'threadId': parent, 'input': [
                {'type': 'text', 'text': 'Seed private transition history.', 'text_elements': []}]})['turn']['id']
            wait_server(lambda: (thread := rpc.call('thread/read', {'threadId': parent,
                'includeTurns': True})['thread'])['status']['type'] == 'idle'
                and thread['turns'][-1]['id'] == seed and thread['turns'][-1]['status'] == 'completed',
                'exact seed turn did not complete')
            assert provider.count == 1, provider.requests
            rpc.call('thread/name/set', {'threadId': parent, 'name': f'private-{scenario}'})
            report.update(parent=parent, seed_turn=seed, initial_generation=rpc.generation)
            previous = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update(env)
                terminal = Terminal(package / 'bin/codex', home, workspace, parent=parent)
            finally:
                os.environ.clear()
                os.environ.update(previous)
            wait([terminal], lambda: composer_ready(terminal.text) and SEED in terminal.text,
                 'old TUI did not attach to the exact seeded root', 30)
            draft = f'CONTROLLED_{scenario}_DRAFT'
            os.write(terminal.master, draft.encode())
            wait([terminal], lambda: draft_visible(terminal, draft), 'idle draft did not render')
            assert provider.count == 1, 'idle old TUI made an unsolicited provider request'
            screen('before-transition')
            if scenario == 'open-idle-restart':
                notifications.extend(rpc.messages)
                rpc.close()
                rpc = None
                stop(daemon)
                screen('old-daemon-stopped')
                daemon, rpc = launch(new_stock, 'new-daemon')
                report['new_generation'] = rpc.generation
                assert rpc.generation != report['initial_generation'], 'daemon generation did not change'

                def reconnected():
                    thread = rpc.call('thread/read', {'threadId': parent})['thread']
                    return (thread['status']['type'] == 'idle' and draft_visible(terminal, draft)
                            and 'Reconnecting to server' not in terminal.text
                            and 'Reconnect failed' not in terminal.text)

                wait([terminal], reconnected, 'old TUI did not automatically reconnect with its draft', 30)
                assert provider.count == 1, 'idle daemon transition replayed or requested model work'
                screen('automatically-reconnected')
            edited = draft + '_EDITED'
            os.write(terminal.master, b'_EDITED')
            wait([terminal], lambda: draft_visible(terminal, edited), 'composer was not editable after attachment')
            screen('editable-composer')
            assert provider.count == 1, 'editing the draft called the model'
            provider.root_steps.append((message(RESPONSE), None))
            os.write(terminal.master, b'\r')
            wait([terminal], lambda: provider.count == 2 and RESPONSE in terminal.text
                 and composer_ready(terminal.text), 'old TUI did not display the controlled response', 30)
            wait_server(lambda: (current := rpc.call('thread/read', {'threadId': parent,
                'includeTurns': True})['thread'])['status']['type'] == 'idle'
                and len(current['turns']) == 2 and current['turns'][-1]['status'] == 'completed',
                'controlled turn did not complete on the official daemon')
            thread = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
            assert len(thread['turns']) == 2 and all(turn['status'] == 'completed' for turn in thread['turns']), thread
            assert thread['turns'][-1]['id'] != seed, thread
            users = [item for item in provider.requests[-1]['body']['input']
                     if item.get('type') == 'message' and item.get('role') == 'user']
            assert any(edited in json.dumps(item) for item in users), users
            assert not rpc.call('thread/queue/list', {'threadId': parent})['data']
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                terminal.read()
                assert terminal.process.poll() is None, terminal.text
                assert provider.count == 2, 'unsolicited or replayed provider request after the controlled response'
            actual_tui = Path(f'/proc/{terminal.process.pid}/exe').resolve(strict=True)
            assert actual_tui == (package / 'bin/codex').resolve(strict=True), actual_tui
            report.update(passed=True, controlled_prompt=edited, seed_requests=1, controlled_requests=1,
                          observed_provider_requests=provider.count, final_thread=thread,
                          old_tui_pid=terminal.process.pid, old_tui_executable=str(actual_tui),
                          old_tui_survived=terminal.process.poll() is None, composer_editable=True)
            screen('controlled-response')
        except BaseException:
            report['failure'] = traceback.format_exc()
            raise
        finally:
            if terminal:
                (evidence / 'terminal.ansi').write_bytes(terminal.output)
                screen('final')
                stop(terminal.process)
                terminal.close()
            if rpc:
                notifications.extend(rpc.messages)
                rpc.close()
            for daemon in daemons:
                stop(daemon)
            stop_rust_observers(home)
            for log in logs:
                log.close()
            for path in home.glob('sessions/**/*.jsonl'):
                shutil.copyfile(path, evidence / path.name)
            if provider:
                (evidence / 'provider-requests.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
                provider.close()
            (evidence / 'rpc-notifications.json').write_text(json.dumps(notifications, indent=2) + '\n')
            survivors = owned_processes(home)
            report['cleanup'] = dict(private_root=str(root), remaining_processes=survivors,
                daemon_returncodes=[daemon.returncode for daemon in daemons],
                tui_returncode=terminal.process.returncode if terminal else None,
                provider_closed=provider is None or (provider.server.fileno() == -1 and not provider.thread.is_alive()))
            report['observed_provider_requests'] = provider.count if provider else 0
            temporary_directory.cleanup()
            report['cleanup']['private_root_removed'] = not root.exists()
            report['cleanup']['passed'] = (not survivors and report['cleanup']['provider_closed']
                                           and report['cleanup']['private_root_removed'])
            report['passed'] = report['passed'] and report['cleanup']['passed']
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            assert report['cleanup']['passed'], report['cleanup']
    assert report['cleanup']['private_root_removed'], root
    print(json.dumps({'passed': report['passed'], 'scenario': scenario, 'evidence': str(evidence)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-package', required=True, type=Path)
    parser.add_argument('--old-manifest-sha256', required=True)
    parser.add_argument('--old-tui-sha256', required=True)
    parser.add_argument('--old-stock-binary', required=True, type=Path)
    parser.add_argument('--new-stock-binary', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--scenario', choices=SCENARIOS + ('all',), default='all')
    args = parser.parse_args()
    os.umask(0o077)
    package = args.old_package.resolve(strict=True)
    old_stock, new_stock = args.old_stock_binary.resolve(strict=True), args.new_stock_binary.resolve(strict=True)
    old_identity, new_identity = official_stock_identity(old_stock), official_stock_identity(new_stock)
    assert old_identity[0] == 'codex-cli 0.163.0' and new_identity[0] == 'codex-cli 0.165.0'
    identities = dict(package=package_identity(package, args.old_manifest_sha256, args.old_tui_sha256),
        old_stock=dict(binary=str(old_stock), version=old_identity[0], sha256=old_identity[1]),
        new_stock=dict(binary=str(new_stock), version=new_identity[0], sha256=new_identity[1]))
    failed = []
    for scenario in SCENARIOS if args.scenario == 'all' else (args.scenario,):
        try:
            run(package, old_stock, new_stock, args.evidence.resolve() / scenario, scenario, identities)
        except Exception:
            traceback.print_exc()
            failed.append(scenario)
    if failed:
        raise SystemExit(f'Failed scenarios: {failed}')
