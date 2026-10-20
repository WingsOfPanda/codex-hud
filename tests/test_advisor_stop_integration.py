"""Actual native /stop must clean the advisor's detached consultation group.

Uses a private TUI, loopback provider and a TERM-resistant consultation stub.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time

from fixtures.native_server import AppServer
from fixtures.shell_policy_provider import ShellPolicyProvider
from native_smoke import Terminal, composer_ready, wait
from test_rust_runtime_advisor_regressions import HELD, MODEL, PARENT
from test_shell_policy_integration import configure, process_running


def run(binary, runtime, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    # The baseline helper leaks children; collect them during a failing run too.
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    report = dict(binary=str(binary), runtime=str(runtime),
                  binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                  runtime_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),
                  remote_model_requests=0, passed=False)
    with tempfile.TemporaryDirectory(prefix='as-') as temporary:
        root = Path(temporary)
        home, workspace, advisor_home, package = [root / name for name in ('h', 'w', 'a', 'p')]
        workspace.mkdir()
        (advisor_home / 'app-server-control').mkdir(parents=True)
        (package / 'bin').mkdir(parents=True)
        stub = package / 'bin/codex'
        stub.write_text('#!/usr/bin/python3\n' + HELD)
        stub.chmod(0o700)
        rpc = AppServer(PARENT, workspace, 0, models=[MODEL],
                        socket_path=advisor_home / 'app-server-control/app-server-control.sock')
        command = '# codex-plus-shell: foreground\n' + shlex.join([
            'env', f'CODEX_HOME={advisor_home}', f'CODEX_THREAD_ID={PARENT}',
            str(runtime), 'run', '--', str(runtime), '--package-root', str(package),
            'advisor', '--provider', 'astra', '--brief', 'Private cancellation fixture; no provider call.'])
        provider = ShellPolicyProvider(command, yield_time_ms=1000)
        configure(home, workspace, provider)
        terminal, pids = None, []
        peer = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
        try:
            terminal = Terminal(binary, home, workspace)
            wait([terminal], lambda: composer_ready(terminal.text), 'private TUI did not initialize', 30)
            os.write(terminal.master, b'Run the fixture.')
            wait([terminal], lambda: 'Run the fixture.' in terminal.text, 'fixture input did not render')
            os.write(terminal.master, b'\r')
            marker = advisor_home / 'owned-pids'
            wait([terminal], lambda: marker.exists() and marker.stat().st_size > 0,
                 'consultation group did not start', 20)
            pids = json.loads(marker.read_text())
            assert all(process_running(pid) for pid in pids), pids
            os.write(terminal.master, b'\x03')
            wait([terminal], lambda: 'Conversation interrupted' in terminal.text,
                 'private turn did not interrupt')
            assert all(process_running(pid) for pid in pids), 'turn interruption stopped the tracked job'
            os.write(terminal.master, b'/stop')
            wait([terminal], lambda: any(line.strip() == '› /stop' for line in terminal.screen.display),
                 'native /stop input did not render')
            os.write(terminal.master, b'\r')
            wait([terminal], lambda: all(not Path(f'/proc/{pid}').exists() for pid in pids),
                 'native /stop left advisor leader or descendant behind', 8)
            assert peer.poll() is None, 'cleanup harmed another process group'
            assert provider.count <= 2, 'cleanup triggered another model request'
            report.update(passed=True, terminated_known_pids=pids, unrelated_group_preserved=True,
                          loopback_provider_requests=provider.count)
        finally:
            provider.final_gate.set()
            if terminal:
                (evidence / 'screen.txt').write_text(terminal.text)
                (evidence / 'terminal.ansi').write_bytes(terminal.output)
                terminal.close()
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 3
            for pid in pids:
                while time.monotonic() < deadline:
                    try:
                        if os.waitpid(pid, os.WNOHANG)[0]:
                            break
                    except ChildProcessError:
                        break
                    time.sleep(.02)
            peer.terminate()
            peer.wait(timeout=5)
            provider.close()
            rpc.close()
            (evidence / 'provider.json').write_text(json.dumps(provider.requests, indent=2))
            (evidence / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.runtime.resolve(strict=True), args.evidence.resolve())
