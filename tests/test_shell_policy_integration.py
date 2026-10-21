"""Real TUI shell policies and stock-daemon helper park, private homes, loopback only.

Run with the native_smoke pyte interpreter: python tests/test_shell_policy_integration.py
    BINARY --evidence .build/shell-policy/acceptance [--stock-daemon OFFICIAL_BINARY]
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from fixtures.shell_policy_provider import ShellPolicyProvider
from native_smoke import Terminal, advisor_rows, assert_compact_hud, composer_ready, wait
from test_background_integration import (REPO, RecordingClient, coordinator_state, health_state,
                                         stop, stop_observers, wait as wait_server)


CASES = ('auto_quick', 'auto_long', 'auto_polling', 'background_quick', 'background_long',
         'unmarked_long', 'foreground_long', 'background_failure')


def configure(home, workspace, provider):
    home.mkdir(mode=0o700)
    workspace.mkdir(exist_ok=True)
    (home / 'config.toml').write_text(f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
suppress_unstable_features_warning=true
check_for_update_on_startup=false
analytics.enabled=false
features.daemon_auto_start=false
features.unified_exec=true
features.shell_tool=true
features.plugins=false
tui.status_line=["thread-name"]
[projects."{workspace}"]
trust_level="trusted"
[model_providers.isolated]
name="loopback shell policy acceptance"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')


def rollout(home):
    return [json.loads(line) for path in (home / 'sessions').glob('**/*.jsonl')
            for line in path.read_text().splitlines() if line]


def turns(home):
    return sum(event.get('type') == 'event_msg' and event.get('payload', {}).get('type')
               in ('task_complete', 'turn_complete') for event in rollout(home))


def parent_id(home):
    return next(event['payload']['id'] for event in rollout(home) if event['type'] == 'session_meta')


def command_for(workspace, marker, held=False, failed=False, *, launcher=None):
    script = workspace / 'finite.py'
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text('import pathlib, sys, time\n'
        'print("POLICY_STARTED", flush=True)\n'
        + (f'deadline = time.monotonic() + 25\n'
           f'while not pathlib.Path({str(workspace / "release")!r}).exists():\n'
           '    assert time.monotonic() < deadline, "finite acceptance command timed out"\n'
           '    time.sleep(.05)\n' if held else '')
        + ('for index in range(40):\n    print(f"FAIL_LINE_{index:03}", flush=True)\n'
           'sys.exit(17)\n' if failed else 'print("POLICY_COMPLETE", flush=True)\n'))
    command = shlex.join([str(launcher or REPO / 'bin/codex-plus'), 'run', '--', sys.executable, str(script)])
    return (f'# codex-plus-shell: {marker}\n' if marker else '') + command


def start_terminal(binary, home, workspace, provider, name):
    terminal = Terminal(binary, home, workspace)
    wait([terminal], lambda: composer_ready(terminal.text), 'TUI did not initialize', 30)
    os.write(terminal.master, f'/rename {name}'.encode())
    wait([terminal], lambda: f'/rename {name}' in terminal.text, 'rename input did not render')
    os.write(terminal.master, b'\r')
    wait([terminal], lambda: name in terminal.text and f'/rename {name}' not in terminal.text,
         'private root name did not finish')
    assert provider.count == 0, 'rename called a model'
    os.write(terminal.master, b'Execute the finite fixture command.')
    wait([terminal], lambda: 'Execute the finite fixture command.' in terminal.text, 'prompt did not render')
    os.write(terminal.master, b'\r')
    return terminal


def completion_rows(text, exit_code=0):
    title = rf'Failed \(exit {exit_code}\)' if exit_code else 'Ran'
    return re.findall(rf'^\s*[•◦]?\s*{title}\s+[^\n]+', text, re.MULTILINE)


def assert_compact_completion(text, exit_code=0):
    assert len(completion_rows(text, exit_code)) == 1, text
    assert 'Show details' in text or re.search(r'^\s*\+ \d+ lines?\b', text, re.MULTILINE), text
    assert 'POLICY_STARTED' not in text and 'POLICY_COMPLETE' not in text, text
    failures = re.findall(r'FAIL_LINE_\d+', text)
    assert failures == (['FAIL_LINE_039'] if exit_code else []), text


def check_shell_details(terminal, log, failed, evidence):
    rows, columns = terminal.screen.lines, terminal.screen.columns
    draft = 'policy-editable-draft' in terminal.text
    replies = terminal.resize(72, 120)
    wait([terminal], lambda: terminal.resized_frame(replies), 'expanded shell geometry did not finish')
    os.write(terminal.master, b'\x1bOS\x1b[C')
    expected = ['POLICY_STARTED'] + ([f'FAIL_LINE_{index:03}' for index in range(40)]
                                      if failed else ['POLICY_COMPLETE'])
    wait([terminal], lambda: 'Show less' in terminal.text and all(line in terminal.text for line in expected)
         and str(log) in ''.join(terminal.text.split()), 'expanded shell lost retained log or output')
    assert 'codex-plus log:' in terminal.text, terminal.text
    assert not draft or 'policy-editable-draft' in terminal.text, terminal.text
    evidence.write_text(terminal.text + '\n')
    # Keep focus on this activity while collapsing, then return input to the composer.
    os.write(terminal.master, b'\x1b[D\x1b[27u')
    wait([terminal], lambda: 'Show less' not in terminal.text, 'shell details did not collapse')
    replies = terminal.resize(rows, columns)
    wait([terminal], lambda: terminal.resized_frame(replies), 'collapsed shell geometry did not finish')
    assert_compact_completion(terminal.text, 17 if failed else 0)
    assert not draft or 'policy-editable-draft' in terminal.text, terminal.text


def run_tui_case(binary, root, evidence, case):
    # Keep CODEX_HOME short enough for the native Unix socket address.
    home, workspace = root / f'h-{CASES.index(case)}', root / case
    marker = case.split('_')[0] if not case.startswith('unmarked') else None
    held, failed = case.endswith('long') or case == 'auto_polling', case.endswith('failure')
    command = command_for(workspace, marker, held, failed)
    polls = 2 if case == 'auto_polling' else 0
    expected_requests = 2 + polls
    provider = ShellPolicyProvider(command, yield_time_ms=1000 if polls else 10000, polls=polls)
    configure(home, workspace, provider)
    terminal = replay = None
    log = None
    try:
        terminal = start_terminal(binary, home, workspace, provider, case.replace('_', '-'))
        started = time.monotonic()
        if held:
            wait([terminal], lambda: provider.count >= 1 and list((home / 'codex-plus/logs').glob('*')),
                 'helper did not start', 30)
            log = next((home / 'codex-plus/logs').glob('*'))
            if marker == 'auto':
                assert 'Background ·' not in terminal.text, 'auto command quieted before its cutoff\n' + terminal.text
                wait([terminal], lambda: 'Background ·' in terminal.text, 'auto command did not become quiet after two seconds', 7)
                assert time.monotonic() - started >= 1.8, terminal.text
            elif marker == 'background':
                wait([terminal], lambda: 'Background ·' in terminal.text, 'explicit background command did not enter HUD', 2)
            else:
                observed = time.monotonic()
                wait([terminal], lambda: time.monotonic() - observed > 2.2, 'foreground observation did not finish', 4)
                if marker == 'foreground':
                    assert 'Background ·' not in terminal.text, terminal.text
                (evidence / f'{case}-running-screen.txt').write_text(terminal.text + '\n')
            if marker in ('auto', 'background'):
                if polls:
                    wait([terminal], lambda: provider.count == expected_requests, 'background polls did not finish', 10)
                    assert 'write_stdin' not in terminal.text and 'Chunk ID:' not in terminal.text, terminal.text
                    assert 'Wall time:' not in terminal.text and 'Process running with session ID' not in terminal.text, terminal.text
                assert 'POLICY_STARTED' not in terminal.text, terminal.text
                os.write(terminal.master, b'policy-editable-draft')
                wait([terminal], lambda: 'policy-editable-draft' in terminal.text and 'Background ·' in terminal.text,
                     'quiet shell prevented composer edits')
                assert advisor_rows(terminal.text) == ['Advisor · ready · 0 calls'], terminal.text
                assert_compact_hud(terminal.text, rows=2, idle=False)
                (evidence / f'{case}-running-screen.txt').write_text(terminal.text + '\n')
            (workspace / 'release').touch()
        provider.final_gate.set()
        wait([terminal], lambda: provider.count == expected_requests and turns(home) == 1 and 'fixture-finished' in terminal.text,
             'shell fixture turn did not finish', 30)
        if log is None:
            logs = list((home / 'codex-plus/logs').glob('*'))
            assert len(logs) == 1, logs
            log = logs[0]
        expected_exit = 17 if failed else 0
        # A yielded process can complete after the assistant's turn has ended.
        wait([terminal], lambda: completion_rows(terminal.text, expected_exit),
             'canonical shell completion did not produce its command row', 5)
        assert_compact_completion(terminal.text, expected_exit)
        completed_at = time.monotonic()
        if 'Background ·' in terminal.text:
            (evidence / f'{case}-pending-refresh-screen.txt').write_text(terminal.text + '\n')
        # The daemon snapshot refreshes once per second after canonical completion.
        wait([terminal], lambda: 'Background ·' not in terminal.text and ' · job ' not in terminal.text,
             'completed shell persisted beyond the HUD refresh interval', 3)
        hud_clear_ms = round((time.monotonic() - completed_at) * 1000)
        assert 'POLICY_STARTED' in log.read_text(), log
        assert ('FAIL_LINE_000' in log.read_text() and 'FAIL_LINE_039' in log.read_text()) if failed else 'POLICY_COMPLETE' in log.read_text()
        assert provider.count == expected_requests, provider.requests
        (evidence / f'{case}-complete-screen.txt').write_text(terminal.text + '\n')
        shutil.copy2(log, evidence / f'{case}-command.log')
        check_shell_details(terminal, log, failed, evidence / f'{case}-expanded-screen.txt')
        assert provider.count == expected_requests, 'local disclosure called a model'
        (evidence / f'{case}-terminal.ansi').write_bytes(terminal.output)
        terminal.close()
        terminal = None
        replay = Terminal(binary, home, workspace, parent=parent_id(home))
        wait([replay], lambda: completion_rows(replay.text, expected_exit) and 'fixture-finished' in replay.text,
             'resume did not reproduce compact command row', 30)
        assert_compact_completion(replay.text, expected_exit)
        assert 'Show less' not in replay.text, 'resume retained manual shell expansion'
        assert provider.count == expected_requests and turns(home) == 1, 'replay called a model or created a turn'
        (evidence / f'{case}-replay-screen.txt').write_text(replay.text + '\n')
        return {'case': case, 'exit': expected_exit, 'compact': True,
                'completion_hud_clear_ms': hud_clear_ms, 'loopback_requests': provider.count}
    finally:
        (workspace / 'release').touch()
        provider.final_gate.set()
        for label, term in (('terminal', terminal), ('replay', replay)):
            if term:
                (evidence / f'{case}-{label}.ansi').write_bytes(term.output)
                (evidence / f'{case}-{label}-screen.txt').write_text(term.text + '\n')
                term.close()
        for path in home.glob('log/*'):
            if path.is_file():
                shutil.copy2(path, evidence / f'{case}-{path.name}')
        (evidence / f'{case}-provider.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        provider.close()


def run_stock_park(binary, root, evidence, launcher=None):
    home, workspace = root / 'stock-home', root / 'stock-workspace'
    launcher = launcher or REPO / 'bin/codex-plus'
    command = command_for(workspace, 'background', held=True, launcher=launcher)
    provider = ShellPolicyProvider(command, yield_time_ms=1000)
    configure(home, workspace, provider)
    env = {key: value for key, value in os.environ.items()
           if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
    env.update(CODEX_HOME=str(home), PYTHONPATH=str(REPO), RUST_LOG='warn')
    native = rpc = None
    with (evidence / 'stock-native.stderr').open('w') as native_log:
        try:
            native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
                stdin=subprocess.DEVNULL, stdout=native_log, stderr=native_log, start_new_session=True)
            socket_path = home / 'app-server-control/app-server-control.sock'
            wait_server(socket_path.exists, 'official private daemon did not start')
            rpc = RecordingClient(socket_path)
            assert not rpc.initialize_result.get('queueIdempotency'), rpc.initialize_result
            parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
            rpc.call('turn/start', {'threadId': parent,
                'input': [{'type': 'text', 'text': 'Execute finite helper command and end turn.', 'text_elements': []}]})
            wait_server(lambda: provider.count == 2, 'official helper command did not yield')
            processes = rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']
            assert len(processes) == 1, processes
            process = processes[0]['processId']
            parked = subprocess.run([str(launcher), 'park', '--parent', parent, '--process', process,
                '--reason', 'isolated helper shell-policy acceptance'], cwd=REPO, env=env,
                capture_output=True, text=True, timeout=15)
            assert parked.returncode == 0, parked.stderr
            provider.final_gate.set()
            wait_server(lambda: rpc.call('thread/read', {'threadId': parent})['thread']['status']['type'] == 'idle',
                        'official root did not become idle')
            wait_server(lambda: (health := health_state(home, parent)) and health['state'] == 'waiting',
                        'official helper park was not monitored')
            before = provider.count
            time.sleep(1)
            assert provider.count == before == 2, provider.requests
            assert rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data']
            (workspace / 'release').touch()
            wait_server(lambda: provider.count == 3, 'official helper completion did not wake once')
            state = wait_server(lambda: (state := coordinator_state(home))['callbacks'] and
                        state['callbacks'][0]['state'] == 'delivered' and state, 'official callback was not delivered')
            thread = rpc.call('thread/read', {'threadId': parent, 'includeTurns': True})['thread']
            wait_server(lambda: rpc.call('thread/read', {'threadId': parent})['thread']['status']['type'] == 'idle',
                        'official follow-up turn did not finish')
            assert len(thread['turns']) == 2 and len(state['callbacks']) == 1, state
            assert state['jobs'][0]['status'] == 'completed', state
            time.sleep(.5)
            assert provider.count == 3, 'duplicate official helper wake'
            logs = list((home / 'codex-plus/logs').glob('*'))
            assert len(logs) == 1 and 'POLICY_COMPLETE' in logs[0].read_text(), logs
            shutil.copy2(logs[0], evidence / 'stock-command.log')
            (evidence / 'stock-ledger.json').write_text(json.dumps(state, indent=2) + '\n')
            return {'official_daemon': str(binary), 'launcher': str(launcher), 'queueIdempotency': False,
                    'loopback_requests': 3, 'callbacks': 1, 'turns': 2, 'passed': True}
        finally:
            (workspace / 'release').touch()
            provider.final_gate.set()
            if rpc:
                (evidence / 'stock-rpc.json').write_text(json.dumps(rpc.messages, indent=2) + '\n')
                rpc.close()
            if native:
                stop(native)
            stop_observers(home)
            (evidence / 'stock-provider.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            provider.close()


def run_parallel_roots(binary, root, evidence):
    fixtures, terminals = [], []
    try:
        for marker in ('background', 'foreground'):
            home, workspace = root / f'p-{marker}', root / f'parallel-{marker}'
            provider = ShellPolicyProvider(command_for(workspace, marker, held=True))
            configure(home, workspace, provider)
            fixtures.append((home, workspace, provider))
            terminals.append(start_terminal(binary, home, workspace, provider, f'parallel-{marker}'))
        background, foreground = terminals
        wait(terminals, lambda: 'Background ·' in background.text and
             all(list((home / 'codex-plus/logs').glob('*')) for home, _, _ in fixtures),
             'concurrent helper commands did not start')
        observed = time.monotonic()
        wait(terminals, lambda: time.monotonic() - observed > 2.2, 'concurrent observation did not finish', 4)
        assert 'Background ·' not in foreground.text and 'parallel-foreground' not in background.text, background.text
        assert 'parallel-background' not in foreground.text, foreground.text
        for index, terminal in enumerate(terminals):
            os.write(terminal.master, f'parallel-draft-{index}'.encode())
        wait(terminals, lambda: all(f'parallel-draft-{index}' in terminal.text
             for index, terminal in enumerate(terminals)), 'parallel policy roots blocked composers')
        assert advisor_rows(background.text) == ['Advisor · ready · 0 calls'], background.text
        assert_compact_hud(background.text, rows=2, idle=False)
        (fixtures[0][1] / 'release').touch()
        fixtures[0][2].final_gate.set()
        wait(terminals, lambda: completion_rows(background.text) and 'Background ·' not in background.text,
             'background completion did not compact independently')
        assert_compact_completion(background.text)
        assert not (fixtures[1][1] / 'release').exists() and turns(fixtures[1][0]) == 0
        assert not completion_rows(foreground.text) and 'parallel-draft-1' in foreground.text, foreground.text
        background_log = next((fixtures[0][0] / 'codex-plus/logs').glob('*'))
        check_shell_details(background, background_log, False, evidence / 'parallel-background-expanded-screen.txt')
        assert 'parallel-draft-0' in background.text and 'parallel-draft-1' in foreground.text
        (fixtures[1][1] / 'release').touch()
        fixtures[1][2].final_gate.set()
        wait(terminals, lambda: completion_rows(foreground.text) and
             all(turns(home) == 1 for home, _, _ in fixtures), 'foreground peer did not finish normally')
        assert_compact_completion(foreground.text)
        foreground_log = next((fixtures[1][0] / 'codex-plus/logs').glob('*'))
        check_shell_details(foreground, foreground_log, False, evidence / 'parallel-foreground-expanded-screen.txt')
        assert 'parallel-draft-0' in background.text and 'parallel-draft-1' in foreground.text
        assert all(provider.count == 2 for _, _, provider in fixtures), 'isolation added model calls'
        return {'parallel_roots': 2, 'isolated_policies': True, 'editable_composers': True}
    finally:
        for _, workspace, provider in fixtures:
            (workspace / 'release').touch()
            provider.final_gate.set()
        for index, terminal in enumerate(terminals):
            (evidence / f'parallel-{index}-screen.txt').write_text(terminal.text + '\n')
            (evidence / f'parallel-{index}.ansi').write_bytes(terminal.output)
            terminal.close()
        for index, (_, _, provider) in enumerate(fixtures):
            (evidence / f'parallel-{index}-provider.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
            provider.close()


def process_running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return False


def run_native_cancellation(binary, root, evidence):
    home, workspace = root / 'cancel-home', root / 'cancel-workspace'
    command = command_for(workspace, 'background')
    script = workspace / 'finite.py'
    script.write_text('import os, pathlib, subprocess, sys, time\n'
        'level = int(sys.argv[1]) if len(sys.argv) > 1 else 0\n'
        f'pathlib.Path({str(workspace)!r}, f"pid-{{level}}").write_text(str(os.getpid()))\n'
        'if level < 2:\n    subprocess.Popen([sys.executable, __file__, str(level + 1)])\n'
        'print("CANCEL_STARTED", flush=True)\ntime.sleep(25)\n')
    provider = ShellPolicyProvider(command)
    configure(home, workspace, provider)
    terminal, pids = None, []
    try:
        terminal = start_terminal(binary, home, workspace, provider, 'cancel-policy')
        wait([terminal], lambda: len(list(workspace.glob('pid-*'))) == 3 and 'Background ·' in terminal.text,
             'native cancellation process family did not start')
        pids = [int(path.read_text()) for path in workspace.glob('pid-*')]
        assert all(process_running(pid) for pid in pids), pids
        os.write(terminal.master, b'\x03')
        wait([terminal], lambda: 'Conversation interrupted' in terminal.text and 'Background ·' in terminal.text,
             'turn interruption lost its tracked background shell')
        # Stock turn interruption preserves processes; /stop owns group termination.
        assert all(process_running(pid) for pid in pids), pids
        os.write(terminal.master, b'/stop')
        wait([terminal], lambda: any(line.strip() == '› /stop' for line in terminal.screen.display),
             'native /stop input did not render')
        os.write(terminal.master, b'\r')
        wait([terminal], lambda: not any(process_running(pid) for pid in pids),
             'native /stop left helper child or grandchild running', 8)
        assert provider.count == 1, 'cancellation continued the model turn'
        logs = list((home / 'codex-plus/logs').glob('*'))
        assert len(logs) == 1 and 'CANCEL_STARTED' in logs[0].read_text(), logs
        shutil.copy2(logs[0], evidence / 'cancel-command.log')
        return {'ctrl_c_preserves_tracked_processes': True, 'native_stop_kills_process_group': True,
                'terminated_known_child_pids': pids, 'partial_log_preserved': True}
    finally:
        provider.final_gate.set()
        if terminal:
            (evidence / 'cancel-screen.txt').write_text(terminal.text + '\n')
            (evidence / 'cancel.ansi').write_bytes(terminal.output)
            terminal.close()
        for pid in pids:
            if process_running(pid):
                os.kill(pid, signal.SIGKILL)
        (evidence / 'cancel-provider.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        provider.close()


def run(binary, evidence, cases=CASES, stock_daemon=None, stock_launcher=None):
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'report.json').unlink(missing_ok=True)
    report = {'binary': str(binary), 'remote_model_requests': 0, 'cases': []}
    with tempfile.TemporaryDirectory(prefix='s-') as temporary:
        root = Path(temporary)
        for case in cases:
            report['cases'].append(run_tui_case(binary, root, evidence, case))
        if tuple(cases) == CASES:
            report['parallel'] = run_parallel_roots(binary, root, evidence)
            report['cancellation'] = run_native_cancellation(binary, root, evidence)
        if stock_daemon:
            report['stock_park'] = run_stock_park(stock_daemon, root, evidence, stock_launcher)
    report['passed'] = True
    (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, default=REPO / '.build/shell-policy/acceptance')
    parser.add_argument('--case', choices=CASES, action='append')
    parser.add_argument('--stock-daemon', type=Path)
    parser.add_argument('--stock-launcher', type=Path,
                        help='Qualified Plus package launcher for the isolated stock park check')
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve(), args.case or CASES,
        args.stock_daemon.resolve() if args.stock_daemon else None,
        args.stock_launcher.resolve() if args.stock_launcher else None)
