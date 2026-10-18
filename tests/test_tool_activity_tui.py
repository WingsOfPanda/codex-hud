"""Overlapping MCP/web activity in a real PTY, with a loopback app-server only.

Run with the native-smoke pyte interpreter:
    python tests/test_tool_activity_tui.py CANDIDATE --evidence EVIDENCE
"""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest

from fixtures.native_server import AppServer, frame
from native_smoke import composer_ready, wait
from test_upstream_0163_tui import private_terminal


class ActivityServer(AppServer):
    def __init__(self, cwd):
        super().__init__('00000000-0000-0000-0016-000000000001', cwd, 0)
        self.notifications = []

    def notify(self, method, **params):
        message = {'method': method, 'params': {'threadId': self.root, **params}}
        self.notifications.append(message)
        payload = frame(json.dumps(message).encode())
        with self.lock:
            assert len(self.connections) == 1, self.connections
            next(iter(self.connections)).sendall(payload)

    def turn(self, status):
        self.notify('turn/started' if status == 'inProgress' else 'turn/completed',
                    turn={'id': 'activity-turn', 'items': [], 'status': status,
                          'error': None, 'startedAt': int(time.time()),
                          'completedAt': None if status == 'inProgress' else int(time.time())})

    def item(self, item, completed=False):
        self.notify('item/completed' if completed else 'item/started',
                    item=item, turnId='activity-turn',
                    **{'completedAtMs' if completed else 'startedAtMs': int(time.time() * 1000)})


def mcp(server, status='inProgress'):
    return {'type': 'mcpToolCall', 'id': server, 'server': server, 'tool': 'search',
            'arguments': {'query': 'fixture query'}, 'status': status,
            'result': {'content': [{'type': 'text', 'text': 'ANYSEARCH_RESULT'}]}
                      if status == 'completed' else None,
            'error': {'message': 'TAVILY_FAILURE'} if status == 'failed' else None,
            'durationMs': None if status == 'inProgress' else 25}


def web():
    return {'type': 'webSearch', 'id': 'web', 'query': 'WEB_RESULT_QUERY',
            'action': {'type': 'search', 'query': 'WEB_RESULT_QUERY'}}


def assert_outputs(text):
    assert not re.search(r'\bCalling\b|\bBrowsing\b|[\u2800-\u28ff]', text), text
    for output in ('ANYSEARCH_RESULT', 'TAVILY_FAILURE', 'WEB_RESULT_QUERY'):
        assert text.count(output) == 1, (output, text)


def assert_settled(text, interrupted=False):
    assert not re.search(r'\bCalling\b|\bBrowsing\b|[\u2800-\u28ff]', text), text
    for server in ('anysearch', 'tavily'):
        headers = [line for line in text.splitlines()
                   if re.search(rf'\b(?:Called|Failed|Interrupted) {server}\.search\b', line)]
        assert len(headers) == 1, (server, headers, text)
    if interrupted:
        assert text.count('Interrupted web search') == 1, text
        assert text.count('Error: interrupted') == 2, text
    else:
        assert_outputs(text)


def activity_case(binary, root, evidence, mode, scenario):
    interrupted = scenario != 'complete'
    label = f'{mode}-{scenario}'
    home, workspace = root / f'{label}-home', root / f'{label}-workspace'
    home.mkdir(mode=0o700)
    workspace.mkdir()
    owned, alternate = {'owned': (True, 'always'), 'terminal': (False, 'always'),
                        'no-alt': (True, 'never')}[mode]
    (home / 'config.toml').write_text(
        'model="gpt-6-sol"\nmodel_provider="openai"\n'
        'suppress_unstable_features_warning=true\ncheck_for_update_on_startup=false\n'
        'analytics.enabled=false\nfeatures.daemon_auto_start=false\n'
        f'tui.fullscreen_transcript={str(owned).lower()}\n'
        f'tui.alternate_screen="{alternate}"\n'
        f'[projects."{workspace}"]\ntrust_level="trusted"\n')
    (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
    server, terminal = ActivityServer(workspace), None
    try:
        terminal = private_terminal(binary, home, workspace, server.url)
        wait([terminal], lambda: composer_ready(terminal.text)
             and any(r.get('method') == 'thread/start' for r in server.requests),
             'private activity terminal did not initialize', 30)
        before_resize = len(terminal.output)
        terminal.resize(50, 120)
        wait([terminal], lambda: len(terminal.output) > before_resize
             and composer_ready(terminal.text),
             'initial PTY resize did not finish')
        server.turn('inProgress')
        for item, pending in ((mcp('anysearch'), 'Calling anysearch.search'),
                              (mcp('tavily'), 'Calling tavily.search'),
                              (web(), 'Browsing')):
            server.item(item)
            wait([terminal], lambda: pending in terminal.text,
                 f'{label}: pending activity did not render: {pending}')
        (evidence / f'{label}-pending-screen.txt').write_text(terminal.text + '\n')
        if not interrupted:
            # Completion order differs from start order, including a failed call.
            for item in (mcp('tavily', 'failed'), web(), mcp('anysearch', 'completed')):
                server.item(item, completed=True)
        server.turn('interrupted' if interrupted else 'completed')
        wait([terminal], lambda: composer_ready(terminal.text)
             and 'esc to interrupt' not in terminal.text.lower(),
             f'{label}: completed turn did not release the composer')
        settled = time.monotonic()
        wait([terminal], lambda: time.monotonic() - settled > 1.2,
             'settled frame observation did not cover an animation refresh', 3)
        assert_settled(terminal.text, interrupted)
        (evidence / f'{label}-settled-screen.txt').write_text(terminal.text + '\n')
        if scenario == 'late-result':
            # Genuine results can arrive after the interrupted row entered terminal history.
            for item in (mcp('anysearch', 'completed'), web(), mcp('tavily', 'failed')):
                server.item(item, completed=True)
            wait([terminal], lambda: all(output in terminal.text for output in
                 ('ANYSEARCH_RESULT', 'TAVILY_FAILURE', 'WEB_RESULT_QUERY')),
                 f'{label}: late authoritative tool output was lost')
            assert_outputs(terminal.text)
            if mode == 'owned':
                assert_settled(terminal.text)
            (evidence / f'{label}-late-screen.txt').write_text(terminal.text + '\n')
        os.write(terminal.master, b'activity-editable-draft')
        wait([terminal], lambda: 'activity-editable-draft' in terminal.text,
             f'{label}: activity rows consumed composer input')
        before_resize = len(terminal.output)
        terminal.resize(50, 100)
        wait([terminal], lambda: len(terminal.output) > before_resize
             and 'activity-editable-draft' in terminal.text,
             f'{label}: resize lost composer input')
        if scenario == 'late-result' and mode != 'owned':
            assert_outputs(terminal.text)
        else:
            assert_settled(terminal.text, scenario == 'interrupt')
        assert not server.errors, server.errors
        assert not any(r.get('method') in ('turn/start', 'turn/steer', 'command/exec')
                       for r in server.requests), server.requests
        return {'case': label, 'passed': True, 'model_calls': 0,
                'notifications': len(server.notifications)}
    finally:
        if terminal:
            (evidence / f'{label}.ansi').write_bytes(terminal.output)
            (evidence / f'{label}-final-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        server.close()
        (evidence / f'{label}-rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')
        (evidence / f'{label}-events.json').write_text(json.dumps(server.notifications, indent=2) + '\n')
        (evidence / f'{label}-errors.json').write_text(json.dumps(server.errors) + '\n')


class ActivityFixtureTests(unittest.TestCase):
    def test_stale_or_duplicate_rows_fail_even_when_outputs_are_present(self):
        settled = ('• Called anysearch.search\n ANYSEARCH_RESULT\n'
                   '• Called tavily.search\n TAVILY_FAILURE\n• Searched WEB_RESULT_QUERY')
        assert_settled(settled)
        for extra in ('\n• Calling anysearch.search', '\n• Browsing web',
                      '\n• Called tavily.search', '\n⠋ Working', '\nANYSEARCH_RESULT'):
            with self.subTest(extra=extra), self.assertRaises(AssertionError):
                assert_settled(settled + extra)

    def test_interruption_keeps_each_tool_outcome(self):
        settled = ('• Failed anysearch.search\n Error: interrupted\n'
                   '• Failed tavily.search\n Error: interrupted\n• Interrupted web search')
        assert_settled(settled, interrupted=True)
        for incomplete in (settled.replace('Interrupted web search', ''),
                           settled.replace('Error: interrupted', '', 1)):
            with self.subTest(incomplete=incomplete), self.assertRaises(AssertionError):
                assert_settled(incomplete, interrupted=True)


def run(binary, evidence, mode, scenario):
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'binary': str(binary), 'passed': [], 'success': False}
    try:
        with tempfile.TemporaryDirectory(prefix='cpa-') as directory:
            for selected_mode in ('owned', 'terminal', 'no-alt') if mode == 'all' else (mode,):
                for selected_scenario in ('complete', 'interrupt', 'late-result') if scenario == 'all' else (scenario,):
                    report['passed'].append(activity_case(binary, Path(directory), evidence,
                                                          selected_mode, selected_scenario))
        report['success'] = True
    finally:
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path, nargs='?')
    parser.add_argument('--evidence', type=Path, default=Path('/tmp/codex-plus-tool-activity'))
    parser.add_argument('--mode', choices=('owned', 'terminal', 'no-alt', 'all'), default='all')
    parser.add_argument('--scenario', choices=('complete', 'interrupt', 'late-result', 'all'), default='all')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        unittest.main(argv=[__file__])
    elif args.binary is None:
        parser.error('binary is required unless --self-test is used')
    else:
        run(args.binary.resolve(), args.evidence.resolve(), args.mode, args.scenario)
