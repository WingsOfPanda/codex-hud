"""Compact shell transcript contract, using private PTYs and loopback responses.

python tests/test_command_display_tui.py BINARY --launcher MATCHING_HELPER --evidence DIR
python tests/test_command_display_tui.py --self-test
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import time
import unittest

from fixtures.shell_policy_provider import ShellPolicyProvider
from native_smoke import Terminal, composer_ready, wait
from test_shell_policy_integration import configure, parent_id, rollout, start_terminal, turns


CASES = ('unmarked_success', 'auto_quick', 'background_success', 'unmarked_failure',
         'background_failure', 'explored', 'running', 'auto_running', 'background_running')
OUTPUT = ('DISPLAY_OUTPUT_START', '{"display_json": "retained"}',
          'def display_code_retained(): pass', 'DISPLAY_OUTPUT_END')
UPDATES = ('DISPLAY_UPDATE_PROGRESS', 'DISPLAY_UPDATE_RELEASE')
FAILURES = tuple(f'DISPLAY_FAILURE_{index:02}' for index in range(10))


def has_disclosure(text):
    return 'Show details' in text or bool(re.search(r'^\s*\+ \d+ lines?\b', text, re.MULTILINE))


def compact_contract(text, *, failed=False, explored=False, running=False):
    """Check rendered rows, never ANSI bytes that may contain an older frame."""
    rows = text.splitlines()
    if explored:
        headers = [row for row in rows if re.match(r'^\s*[•◦]?\s*Explor(?:ed|ing)\b', row)]
    else:
        title = 'Running' if running else 'Failed' if failed else 'Ran'
        headers = [row for row in rows if re.match(rf'^\s*[•◦]?\s*{title}\b', row)]
    assert len(headers) == 1, f'expected one actual command header, got {headers!r}\n{text}'
    assert not any(marker in text for marker in (*OUTPUT, *UPDATES)), f'success stdout/JSON/code preview leaked\n{text}'
    assert 'COMMAND_BODY_RETAINED' not in text, f'multiline command body leaked\n{text}'
    assert has_disclosure(text), f'per-activity disclosure is absent\n{text}'
    if failed:
        assert re.search(r'exit\s*17\b', text), f'factual exit code is absent\n{text}'
        excerpts = re.findall(r'DISPLAY_FAILURE_\d+', text)
        assert len(excerpts) == 1, f'expected one failure excerpt: {excerpts!r}\n{text}'
        assert FAILURES[0] not in text, f'full failure output was expanded\n{text}'
    else:
        assert not any(marker in text for marker in FAILURES), text
    return headers[0].strip()


def snapshot(terminal, evidence, name):
    (evidence / f'{name}.txt').write_text(terminal.text + '\n')
    (evidence / f'{name}.ansi').write_bytes(terminal.output)


def disclosure(terminal, expanded):
    # F4 focuses the last activity; Right/Left reuse its existing local disclosure.
    os.write(terminal.master, b'\x1bOS')
    os.write(terminal.master, b'\x1b[C' if expanded else b'\x1b[D')
    wait([terminal], lambda: 'Show less' in terminal.text if expanded else has_disclosure(terminal.text),
         'existing per-activity disclosure did not toggle')
    os.write(terminal.master, b'\x1b[27u')


def fixture_command(workspace, case, launcher):
    failed, held = case.endswith('failure'), case == 'running' or case.endswith('_running')
    workspace.mkdir()
    if case == 'explored':
        (workspace / 'inspect.txt').write_text('\n'.join(OUTPUT) + '\n')
        return 'cat inspect.txt', list(OUTPUT)
    script = workspace / 'finite.py'
    body = 'import pathlib, sys, time\n'
    if held:
        body += ('pathlib.Path("started").touch()\n'
                 f'print({OUTPUT[0]!r}, flush=True)\n'
                 'deadline = time.monotonic() + 35\n'
                 'for gate in ("progress", "release"):\n'
                 '    while not pathlib.Path(gate).exists():\n'
                 '        assert time.monotonic() < deadline, "finite display fixture timed out"\n'
                 '        time.sleep(.03)\n'
                 '    print("DISPLAY_UPDATE_" + gate.upper(), flush=True)\n')
    lines = FAILURES if failed else OUTPUT[1:] if held else OUTPUT
    body += ''.join(f'print({line!r}, flush=True)\n' for line in lines)
    body += f'sys.exit({17 if failed else 0})\n'
    script.write_text(body)
    command = shlex.join([sys.executable, str(script)]) + ' # ' + 'long-command ' * 14
    command += '\n# COMMAND_BODY_RETAINED'
    marker = 'background' if case.startswith('background') else 'auto' if case.startswith('auto_') else None
    if marker:
        command = (f'# codex-plus-shell: {marker}\n# codex-plus-task: Verify compact command display\n'
                   + shlex.join([str(launcher), 'run', '--', 'bash', '-c', command]))
    return command, list(FAILURES if failed else OUTPUT)


def run_case(binary, launcher, root, evidence, case):
    evidence.mkdir(parents=True)
    home, workspace = root / 'h', root / 'w'
    command, retained = fixture_command(workspace, case, launcher)
    held = case == 'running' or case.endswith('_running')
    # Managed running cases remain inside one tool call across the two-second UI cutoff.
    provider = ShellPolicyProvider(command, yield_time_ms=1000 if case == 'running' else 10000)
    configure(home, workspace, provider)
    config = home / 'config.toml'
    config.write_text('tui.fullscreen_transcript=true\ntui.alternate_screen="always"\n' + config.read_text())
    terminal = replay = None
    stages = []
    report = {'case': case, 'passed': False, 'stages': stages}
    try:
        terminal = start_terminal(binary, home, workspace, provider, 'display-' + case.replace('_', '-'))
        terminal.resize(50, 120)
        stages.append('private-boot-and-loopback-turn')
        if held:
            if case == 'running':
                wait([terminal], lambda: 'Running ' in terminal.text and provider.count >= 1,
                     'real finite command did not enter Running state', 15)
            else:
                wait([terminal], lambda: (workspace / 'started').exists() and provider.count == 1
                     and ('Running ' in terminal.text or 'Background ·' in terminal.text),
                     'managed finite process did not enter running display', 15)
                stages.append('real-managed-process-started')
                if case == 'auto_running':
                    observed = time.monotonic()
                    wait([terminal], lambda: time.monotonic() - observed >= 2.2,
                         'auto visibility cutoff observation did not finish', 4)
                    assert not (workspace / 'release').exists() and provider.count == 1
                    stages.append('auto-cutoff-elapsed-with-live-process')
                    snapshot(terminal, evidence, 'after-auto-cutoff')
                snapshot(terminal, evidence, 'managed-running-before-contract')
            compact_contract(terminal.text, running=True)
            snapshot(terminal, evidence, 'running-collapsed')
            disclosure(terminal, True)
            # Stock early bytes can be completion-only; see async_watcher_tests::completed_output_preserves_bytes_before_subscription.
            (workspace / 'progress').touch()
            wait([terminal], lambda: 'DISPLAY_UPDATE_PROGRESS' in terminal.text,
                 'expansion did not survive live output update')
            assert 'Show less' in terminal.text, terminal.text
            snapshot(terminal, evidence, 'running-expanded-update')
            stages.append('running-collapse-expand-and-update')
            (workspace / 'release').touch()
        provider.final_gate.set()
        wait([terminal], lambda: provider.count == 2 and turns(home) == 1 and 'fixture-finished' in terminal.text,
             'loopback command turn did not finish', 30)
        if case.startswith('background') or case == 'auto_running':
            wait([terminal], lambda: 'Background ·' not in terminal.text, 'completed background HUD did not clear', 5)
        if held:
            assert 'Show less' in terminal.text and OUTPUT[-1] in terminal.text, terminal.text
            stages.append('expansion-survives-completion')
            disclosure(terminal, False)
        snapshot(terminal, evidence, 'complete-before-contract')
        failed, explored = case.endswith('failure'), case == 'explored'
        header = compact_contract(terminal.text, failed=failed, explored=explored)
        stages.append('compact-completion')
        if not explored:
            assert '…' in header, f'overflowing actual command was not clipped\n{terminal.text}'
        replies = terminal.resize(32, 62)
        wait([terminal], lambda: terminal.resized_frame(replies) and composer_ready(terminal.text),
             'narrow transcript frame did not finish')
        compact_contract(terminal.text, failed=failed, explored=explored)
        os.write(terminal.master, b'display-draft')
        wait([terminal], lambda: 'display-draft' in terminal.text, 'collapsed activity blocked composer')
        snapshot(terminal, evidence, 'narrow-editable')
        stages.append('narrow-one-header-and-editable-composer')
        replies = terminal.resize(50, 120)
        wait([terminal], lambda: terminal.resized_frame(replies) and 'display-draft' in terminal.text,
             'wide transcript did not retain draft')
        disclosure(terminal, True)
        wait([terminal], lambda: all(line in terminal.text for line in retained),
             'Show details lost retained output')
        if not explored:
            assert 'COMMAND_BODY_RETAINED' in terminal.text, 'Show details lost full multiline command'
        assert 'display-draft' in terminal.text, 'disclosure lost composer draft'
        snapshot(terminal, evidence, 'expanded')
        stages.append('local-disclosure-retains-command-and-output')
        events = rollout(home)
        call_records = [event['payload'] for event in events
                        if event.get('type') == 'response_item' and event.get('payload', {}).get('type') == 'function_call'
                        and event['payload'].get('name') == 'exec_command']
        assert len(call_records) == 1 and json.loads(call_records[0]['arguments'])['cmd'] == command, 'display mutated or duplicated the executed command'
        report['command_call_id'] = call_records[0]['call_id']
        outputs = [item.get('output', '') for item in provider.requests[1]['body'].get('input', [])
                   if item.get('type') == 'function_call_output']
        assert all(any(line in output for output in outputs) for line in retained), 'model-visible output was folded'
        stored_outputs = [event['payload']['output'] for event in events
                          if event.get('type') == 'response_item'
                          and event.get('payload', {}).get('type') == 'function_call_output']
        assert stored_outputs == outputs, 'stored tool output differs from model-visible tool output'
        stages.append('raw-command-and-model-visible-output-preserved')
        terminal.close()
        terminal = None
        replay = Terminal(binary, home, workspace, parent=parent_id(home))
        replay.resize(50, 120)
        wait([replay], lambda: 'fixture-finished' in replay.text and composer_ready(replay.text),
             'private resume did not render retained turn', 30)
        compact_contract(replay.text, failed=failed, explored=explored)
        assert 'Show less' not in replay.text, 'manual expansion persisted across restart'
        assert provider.count == 2 and turns(home) == 1, 'display/resume added a model request or turn'
        replay_outputs = [event['payload']['output'] for event in rollout(home)
                          if event.get('type') == 'response_item'
                          and event.get('payload', {}).get('type') == 'function_call_output']
        assert replay_outputs == stored_outputs, 'resume changed the retained tool output'
        replay_calls = [event['payload'] for event in rollout(home)
                        if event.get('type') == 'response_item' and event.get('payload', {}).get('type') == 'function_call'
                        and event['payload'].get('name') == 'exec_command']
        assert replay_calls == call_records, 'resume changed the retained command identity or arguments'
        snapshot(replay, evidence, 'restart-collapsed')
        stages.append('restart-collapsed-zero-display-model-calls')
        report.update(passed=True, loopback_requests=provider.count, display_model_requests=0)
        logs = list((home / 'codex-plus/logs').glob('*'))
        for index, log in enumerate(logs):
            shutil.copy2(log, evidence / f'command-{index}.log')
        return report
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        for gate in ('progress', 'release'):
            (workspace / gate).touch()
        provider.final_gate.set()
        for name, term in (('terminal', terminal), ('replay', replay)):
            if term:
                snapshot(term, evidence, name + '-last')
                term.close()
        (evidence / 'provider.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        (evidence / 'case-report.json').write_text(json.dumps(report, indent=2) + '\n')
        provider.close()


class ContractSelfTest(unittest.TestCase):
    def test_success_rejects_output_and_multiline_body(self):
        valid = '• Ran python finite.py …\n  + Show details'
        compact_contract(valid)
        compact_contract(valid.replace('Show details', '4 lines (^t to expand)'))
        for marker in (*OUTPUT, *UPDATES, 'COMMAND_BODY_RETAINED'):
            with self.assertRaises(AssertionError):
                compact_contract(valid + '\n' + marker)

    def test_failure_needs_exit_and_bounded_excerpt(self):
        valid = '• Failed (exit 17) python finite.py …\nDISPLAY_FAILURE_09\n+ Show details'
        compact_contract(valid, failed=True)
        for invalid in (valid.replace('exit 17', 'error'), valid + '\n' + '\n'.join(FAILURES)):
            with self.assertRaises(AssertionError):
                compact_contract(invalid, failed=True)

    def test_header_and_disclosure_are_required(self):
        for invalid in ('Shell completed (exit 0)\n+ Show details', '• Ran python finite.py …',
                        '• Ran one\n• Ran two\n+ Show details'):
            with self.assertRaises(AssertionError):
                compact_contract(invalid)
        compact_contract('• Explored\n+ Show details', explored=True)
        compact_contract('• Running python finite.py …\n+ Show details', running=True)


def run(binary, launcher, evidence, cases):
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'binary': str(binary), 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'launcher': str(launcher), 'requested_cases': list(cases), 'cases': [], 'passed': False,
              'scope': 'shell transcript only; private CODEX_HOME and loopback provider'}
    try:
        for case in cases:
            with tempfile.TemporaryDirectory(prefix='plus-display-') as temporary:
                report['cases'].append(run_case(binary, launcher, Path(temporary), evidence / case, case))
        report['passed'] = True
    finally:
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path, nargs='?')
    parser.add_argument('--launcher', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--cases', nargs='+', choices=CASES, default=CASES)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        unittest.main(argv=[sys.argv[0]])
    else:
        if not args.binary or not args.launcher or not args.evidence:
            parser.error('binary, --launcher, and --evidence are required')
        run(args.binary.resolve(strict=True), args.launcher.resolve(strict=True), args.evidence.resolve(), args.cases)
