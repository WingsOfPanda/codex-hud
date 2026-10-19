"""Focused 0.163 PTY acceptance; private homes and loopback fixtures only.

Run with the native_smoke pyte interpreter:
    python tests/test_upstream_0163_tui.py CANDIDATE --evidence EVIDENCE
Parser/fixture checks without a candidate:
    python tests/test_upstream_0163_tui.py --self-test
"""

import argparse
import base64
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
import unittest
from unittest.mock import patch

from wcwidth import wcswidth

from fixtures.native_server import AppServer
from fixtures.shell_policy_provider import ShellPolicyProvider
from native_smoke import Terminal, advisor_rows, assert_compact_hud, composer_ready, shell_rows, wait, worker_rows
from test_shell_policy_integration import REPO, command_for, configure, rollout, start_terminal, turns


def program_statuses(output):
    return [state.decode() for state in re.findall(
        rb'\x1b\]7501;state=(idle|working|blocked):app=codex\x1b\\', output)]


def clipboard_payloads(output):
    return [base64.b64decode(payload, validate=True).decode() for payload in re.findall(
        rb'\x1b\]52;c;([A-Za-z0-9+/=]*)(?:\x07|\x1b\\)', output)]


def private_terminal(*args, **kwargs):
    # Clipboard requests stay in the private PTY; no desktop/WSL clipboard is reachable.
    env = {key: value for key, value in os.environ.items() if key not in (
        'DISPLAY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'WSL_DISTRO_NAME',
        'WSL_INTEROP', 'HERDR_ENV', 'SSH_CONNECTION', 'SSH_CLIENT')}
    env['SSH_TTY'] = '/dev/pts/isolated-fixture'
    with patch.dict(os.environ, env, clear=True):
        return Terminal(*args, **kwargs)


def footer_position(terminal, token='gpt-6-sol'):
    for row, line in enumerate(terminal.screen.display):
        if token in line:
            return wcswidth(line[:line.index(token)]), row
    raise AssertionError(f'native footer token {token!r} is absent\n{terminal.text}')


def copy_footer(terminal, evidence, label):
    column, row = footer_position(terminal)
    before = len(terminal.output)
    # SGR mouse positions are one-based; the endpoint is an exclusive text anchor.
    for button, x, final in ((0, column, 'M'), (32, column + 9, 'M'),
                              (0, column + 9, 'm')):
        os.write(terminal.master, f'\x1b[<{button};{x + 1};{row + 1}{final}'.encode())
    os.write(terminal.master, b'\x03')
    wait([terminal], lambda: clipboard_payloads(terminal.output[before:]),
         'native footer selection did not emit its private OSC 52 copy')
    copied = clipboard_payloads(terminal.output[before:])
    assert copied == ['gpt-6-sol'], copied
    (evidence / f'{label}-screen.txt').write_text(terminal.text + '\n')
    # Kitty's explicit Escape avoids folding the next character into an Alt chord.
    os.write(terminal.master, b'\x1b[27u')
    return {'row': row, 'column': column, 'copied': copied[0]}


def config_and_selection(binary, root, evidence):
    home, workspace = root / 'remote-home', root / 'remote-workspace'
    home.mkdir(mode=0o700)
    workspace.mkdir()
    (home / 'config.toml').write_text(
        'model="gpt-6-sol"\nmodel_provider="openai"\n'
        'suppress_unstable_features_warning=true\ncheck_for_update_on_startup=false\n'
        'analytics.enabled=false\nfeatures.daemon_auto_start=false\n'
        'tui.copy_on_select="never"\ntui.fullscreen_transcript=true\n'
        'tui.alternate_screen="always"\n'
        f'[projects."{workspace}"]\ntrust_level="trusted"\n')
    (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
    server = AppServer('00000000-0000-0000-0016-000000000001', workspace, 9)
    terminal = None
    geometry = {}
    try:
        terminal = private_terminal(binary, home, workspace, server.url)
        wait([terminal], lambda: '+4 more' in terminal.text and composer_ready(terminal.text),
             'six-row HUD and native footer did not initialize', 30)
        assert_compact_hud(terminal.text, rows=6)
        geometry['six_rows'] = copy_footer(terminal, evidence, 'selection-six-rows')

        draft = 'config-preserved-draft'
        os.write(terminal.master, draft.encode())
        wait([terminal], lambda: draft in terminal.text, 'selection consumed composer input')
        # The native kill buffer is the supported way to run a slash command mid-draft.
        os.write(terminal.master, b'\x01\x0b')
        wait([terminal], lambda: draft not in terminal.text and composer_ready(terminal.text),
             'draft did not enter the native kill buffer')
        os.write(terminal.master, b'/config')
        wait([terminal], lambda: '/config' in terminal.text, '/config input did not render')
        os.write(terminal.master, b'\r')
        wait([terminal], lambda: 'Configuration' in terminal.text and 'Theme' in terminal.text,
             '/config did not open the upstream configuration catalog')
        polls = sum(request.get('method') == 'thread/list' for request in server.requests)
        server.reject_turn_metadata = True
        wait([terminal], lambda: sum(request.get('method') == 'thread/list'
             for request in server.requests) > polls, 'HUD polling stopped while /config was open')
        assert 'Configuration' in terminal.text, terminal.text
        (evidence / 'config-active-refresh-screen.txt').write_text(terminal.text + '\n')
        os.write(terminal.master, b'\x1b[27u')
        wait([terminal], lambda: 'Configuration' not in terminal.text and 'Workers stale' in terminal.text,
             '/config did not close onto the refreshed warning/overflow HUD')
        assert_compact_hud(terminal.text, rows=6)
        os.write(terminal.master, b'\x19')
        wait([terminal], lambda: draft in terminal.text, '/config lost the native killed draft')
        assert_compact_hud(terminal.text, rows=6)
        (evidence / 'config-restored-draft-screen.txt').write_text(terminal.text + '\n')

        os.write(terminal.master, b'\x01\x0b')
        wait([terminal], lambda: draft not in terminal.text and composer_ready(terminal.text),
             'draft did not return to kill buffer before geometry check')
        server.reject_turn_metadata = False
        for worker in server.workers:
            worker['status'] = {'type': 'idle'}
            server.turns[worker['id']][0].update(status='completed', completedAt=int(time.time()))
        wait([terminal], lambda: not worker_rows(terminal.text) and 'Workers stale' not in terminal.text
             and '+4 more' not in terminal.text, 'completed workers did not collapse the HUD')
        assert_compact_hud(terminal.text, rows=0)
        geometry['zero_rows'] = copy_footer(terminal, evidence, 'selection-zero-rows')
        assert geometry['six_rows']['row'] != geometry['zero_rows']['row'], geometry

        server.shells = [server.shell(163, 'footer-geometry-held-command')]
        wait([terminal], lambda: len(shell_rows(terminal.text)) == 1,
             'shell snapshot did not restore one HUD row')
        replies = terminal.resize(12, 60)
        wait([terminal], lambda: terminal.resized_frame(replies) and composer_ready(terminal.text)
             and shell_rows(terminal.text), 'narrow footer/HUD frame did not finish')
        assert_compact_hud(terminal.text, rows=1)
        geometry['narrow_one_row'] = copy_footer(terminal, evidence, 'selection-narrow-one-row')
        os.write(terminal.master, b'\x19-edited')
        wait([terminal], lambda: draft + '-edited' in terminal.text,
             'resized footer selection lost the preserved draft or composer ownership')
        assert_compact_hud(terminal.text, rows=1)
        (evidence / 'selection-narrow-edited-screen.txt').write_text(terminal.text + '\n')
        assert not server.errors, server.errors
        assert not any(request.get('method') in ('turn/start', 'turn/steer', 'command/exec')
                       for request in server.requests), server.requests
        return {'case': 'config_and_footer_selection', 'geometry': geometry,
                'ui_started_turns': 0, 'model_calls': 0, 'passed': True}
    finally:
        if terminal:
            (evidence / 'config-selection.ansi').write_bytes(terminal.output)
            (evidence / 'config-selection-final-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        server.close()
        (evidence / 'config-selection-rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')
        (evidence / 'config-selection-fixture-errors.json').write_text(json.dumps(server.errors) + '\n')


def managed_shell_status(binary, root, evidence):
    home, workspace = root / 'status-home', root / 'status-workspace'
    package_bin = root / 'status-package/bin'
    package_bin.mkdir(parents=True)
    for name in ('codex', 'codex-code-mode-host', 'codex-plus-runtime'):
        artifact = binary if name == 'codex' else binary.with_name(name)
        (package_bin / name).symlink_to(artifact.resolve(strict=True))
    launcher = package_bin / 'codex-plus'
    # Copy the launcher: its readlink-based root must resolve to this private package.
    shutil.copy2(REPO / 'bin/codex-plus', launcher)
    provider = ShellPolicyProvider(command_for(workspace, 'background', held=True, launcher=launcher),
                                   yield_time_ms=1000)
    configure(home, workspace, provider)
    terminal = None
    try:
        # start_terminal uses the same safe child environment for its embedded daemon.
        with patch('test_shell_policy_integration.Terminal', private_terminal):
            terminal = start_terminal(package_bin / 'codex', home, workspace, provider, 'upstream-status-acceptance')
        wait([terminal], lambda: provider.count == 2 and shell_rows(terminal.text),
             'marked background command did not yield while held', 30)
        assert not (workspace / 'release').exists()
        wait([terminal], lambda: program_statuses(terminal.output)
             and program_statuses(terminal.output)[-1] == 'working',
             'held managed command did not report OSC 7501 Working')
        provider.final_gate.set()
        wait([terminal], lambda: turns(home) == 1 and 'fixture-finished' in terminal.text,
             'parent fixture turn did not become idle with its shell still held', 30)
        os.write(terminal.master, b'status-editable-draft')
        wait([terminal], lambda: 'status-editable-draft' in terminal.text,
             'held command blocked the idle composer')
        observed = time.monotonic()
        wait([terminal], lambda: time.monotonic() - observed >= 2.2,
             'idle shell observation did not cover a HUD refresh', 4)
        assert shell_rows(terminal.text), terminal.text
        assert len(advisor_rows(terminal.text)) == 1, terminal.text
        assert '(xhigh) · ready · 0 calls' in advisor_rows(terminal.text)[0], terminal.text
        assert_compact_hud(terminal.text, rows=2)
        assert program_statuses(terminal.output)[-1] == 'working', program_statuses(terminal.output)
        assert 'esc to interrupt' not in terminal.text.lower(), terminal.text
        assert not re.search(r'(?m)^\s*[•◦●⠁-⣿].*\bWorking\b', terminal.text), terminal.text
        assert provider.count == 2 and turns(home) == 1, provider.requests
        (evidence / 'status-working-idle-parent-screen.txt').write_text(terminal.text + '\n')
        completion_offset = len(terminal.output)
        (workspace / 'release').touch()
        wait([terminal], lambda: not shell_rows(terminal.text)
             and program_statuses(terminal.output[completion_offset:])
             and program_statuses(terminal.output[completion_offset:])[-1] == 'idle',
             'completed managed command did not clear its HUD and emit OSC 7501 Idle', 15)
        assert program_statuses(terminal.output[completion_offset:]) == ['idle'], program_statuses(terminal.output)
        assert 'status-editable-draft' in terminal.text, terminal.text
        assert_compact_hud(terminal.text, rows=1)
        assert provider.count == 2 and turns(home) == 1, 'display refresh/completion started an extra model turn'
        logs = list((home / 'codex-plus/logs').glob('*'))
        assert len(logs) == 1 and 'POLICY_COMPLETE' in logs[0].read_text(), logs
        shutil.copy2(logs[0], evidence / 'status-command.log')
        with (package_bin / 'codex-plus-runtime').open('rb') as stream:
            runtime_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'case': 'marked_background_program_status', 'statuses': program_statuses(terminal.output),
                'runtime_sha256': runtime_sha256,
                'loopback_requests': provider.count, 'turns': turns(home), 'passed': True}
    finally:
        (workspace / 'release').touch()
        provider.final_gate.set()
        if terminal:
            (evidence / 'status-shell.ansi').write_bytes(terminal.output)
            (evidence / 'status-shell-final-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        (evidence / 'status-provider.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        (evidence / 'status-rollout.json').write_text(json.dumps(rollout(home), indent=2) + '\n')
        for path in home.glob('log/*'):
            if path.is_file():
                shutil.copy2(path, evidence / f'status-{path.name}')
        provider.close()


class AcceptanceFixtureTests(unittest.TestCase):
    def test_command_launcher_is_explicit_or_keeps_the_repository_default(self):
        with tempfile.TemporaryDirectory(prefix='plus-launcher-') as temporary:
            workspace = Path(temporary)
            explicit = workspace / 'private package/bin/codex-plus'
            for launcher in (None, explicit):
                command = command_for(workspace, 'background', launcher=launcher)
                marker, invocation = command.split('\n', 1)
                self.assertEqual(marker, '# codex-plus-shell: background')
                self.assertEqual(shlex.split(invocation)[:3],
                                 [str(launcher or REPO / 'bin/codex-plus'), 'run', '--'])

    def test_status_parser_requires_complete_codex_protocol_and_keeps_order(self):
        data = (b'\x1b]7501;state=working:app=other\x1b\\'
                b'\x1b]7501;state=working:app=codex\x1b\\'
                b'\x1b]7501;state=idle:app=codex\x1b\\'
                b'\x1b]7501;state=blocked:app=codex\x1b')
        self.assertEqual(program_statuses(data), ['working', 'idle'])

    def test_clipboard_parser_accepts_both_terminators_and_ignores_other_osc(self):
        data = b'\x1b]52;c;c2hvcnRjdXRz\x07\x1b]52;c;aGVsbG8=\x1b\\\x1b]0;title\x07'
        self.assertEqual(clipboard_payloads(data), ['shortcuts', 'hello'])


def run(binary, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    version = subprocess.run([str(binary), '--version'], text=True, capture_output=True,
                             check=True, timeout=10).stdout.strip()
    assert re.search(r'\b0\.163\.0\b', version), f'expected fresh 0.163.0 candidate, got {version}'
    report = {'binary': str(binary), 'version': version, 'passed': [], 'success': False}
    try:
        # Short private paths also fit the embedded daemon Unix socket limit on ARM.
        with tempfile.TemporaryDirectory(prefix='cp163-') as directory:
            root = Path(directory)
            report['passed'].append(config_and_selection(binary, root, evidence))
            report['passed'].append(managed_shell_status(binary, root, evidence))
        report['success'] = True
    finally:
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path, nargs='?')
    parser.add_argument('--evidence', type=Path, default=Path('/tmp/codex-plus-upstream-0163-evidence'))
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        unittest.main(argv=[__file__])
    elif args.binary is None:
        parser.error('binary is required unless --self-test is used')
    else:
        run(args.binary.resolve(), args.evidence.resolve())
