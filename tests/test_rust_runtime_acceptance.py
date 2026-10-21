"""Black-box Rust coordinator gates with the unchanged Python 0.2 ledger oracle.

Run: python3 tests/test_rust_runtime_acceptance.py --runtime PATH
Only private CODEX_HOME directories and the existing Unix RPC fixture are used.
The Python test driver is not part of the shipped runtime.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from codex_plus.background import Observer, Store, register
from codex_plus.rpc import Client
from fixtures.native_server import AppServer
from test_background import CHILD, PARENT, Fake, NativeFake


RUNTIME = Path(os.environ['CODEX_PLUS_TEST_RUNTIME']).resolve() if os.environ.get('CODEX_PLUS_TEST_RUNTIME') else None


def wait(check, reason, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError(reason)


class RuntimeServer(AppServer):
    """Use the existing WebSocket wire fixture and existing lifecycle fake."""

    def __init__(self, home, fake):
        self.fake = fake
        self.drop_after_method = None
        self.lost_ack = threading.Event()
        self.hold_before_method = None
        self.method_entered = threading.Event()
        self.release_method = threading.Event()
        self.discard_held = False
        socket = home / 'app-server-control/app-server-control.sock'
        socket.parent.mkdir(parents=True)
        super().__init__(PARENT, home, 0, socket_path=socket)
        client = Client(socket)
        self.fake.generation = client.generation
        client.close()

    def reply(self, method, params):
        if method == 'initialize':
            return {'result': getattr(self.fake, 'initialize_result', {})}
        if method == self.hold_before_method:
            self.hold_before_method = None
            self.method_entered.set()
            assert self.release_method.wait(10), 'held RPC fixture timed out'
            if self.discard_held:
                return {'error': {'code': -32603, 'message': 'fixture discarded unexecuted mutation'}}
        try:
            result = self.fake.call(method, params)
            if method == self.drop_after_method:
                self.drop_after_method = None
                self.disconnect()
                self.available.set()
                self.lost_ack.set()
            return {'result': result}
        except (ValueError, TimeoutError, ConnectionError) as error:
            return {'error': {'code': -32603, 'message': str(error)}}

    def close(self):
        self.release_method.set()
        super().close()


class PagedHistoryFake(Fake):
    def __init__(self):
        super().__init__()
        self.history_error = False
        self.item_replies = {}

    def call(self, method, params):
        if method == 'thread/items/list':
            self.calls.append((method, dict(params)))
            turn = params.get('turnId')
            if self.history_error and not turn:
                raise ConnectionError('history transport failed')
            key = (turn, params.get('cursor'))
            if key in self.item_replies:
                reply = self.item_replies[key]
                if isinstance(reply, Exception):
                    raise reply
                return reply
            entries = [entry for entry in self.entries if not turn or entry['turnId'] == turn]
            start = int(params.get('cursor', '0'))
            end = start + params['limit']
            return {'data': entries[start:end],
                    'nextCursor': str(end) if end < len(entries) else None}
        return super().call(method, params)


class PrivateRuntime:
    def __init__(self, fake=None):
        self.temporary = tempfile.TemporaryDirectory(prefix='rr-')
        self.home = Path(self.temporary.name)
        self.runtime = self.home / 'package/bin/codex-plus-runtime'
        self.runtime.parent.mkdir(parents=True)
        shutil.copy2(RUNTIME, self.runtime)
        self.fake = fake or Fake()
        self.server = RuntimeServer(self.home, self.fake)
        self.env = {key: value for key, value in os.environ.items()
                    if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        self.env.update(CODEX_HOME=str(self.home), CODEX_THREAD_ID=PARENT)
        self.logs = []
        self.children = []
        # Park's real launcher runs, but the fixture controls the single observer.
        directory = self.home / 'codex-plus'
        directory.mkdir(mode=0o700)
        self.supervisor_lock = (directory / f'{PARENT}.supervisor.lock').open('w')
        fcntl.flock(self.supervisor_lock, fcntl.LOCK_EX)

    def cli(self, *args, ok=True):
        result = subprocess.run([str(self.runtime), *args], cwd=REPO, env=self.env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.logs.append({'args': list(args), 'returncode': result.returncode,
                          'stdout': result.stdout, 'stderr': result.stderr})
        if ok:
            assert result.returncode == 0, self.logs[-1]
        return result

    def park(self, *selectors):
        result = self.cli('park', '--parent', PARENT, *selectors, '--reason', 'private Rust acceptance')
        return json.loads(result.stdout)

    def state(self):
        store = Store(self.home)
        try:
            return store.status(PARENT)
        finally:
            store.close()

    def status(self):
        return json.loads(self.cli('park', '--status', '--parent', PARENT).stdout)

    def observe(self):
        log = (self.home / f'observer-{len(self.children)}.log').open('w+')
        child = subprocess.Popen([str(self.runtime), 'observe', '--parent', PARENT], cwd=REPO,
                                 env=self.env, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=log, start_new_session=True)
        self.children.append((child, log))
        return child

    def claimed(self):
        return any(job['observer_run'] for job in self.state()['jobs'])

    def delivered(self):
        callbacks = self.state()['callbacks']
        return callbacks and all(row['state'] == 'delivered' for row in callbacks)

    def close(self):
        # Include detached supervisors launched by the actual park CLI.
        owned = set(child.pid for child, _ in self.children)
        for entry in Path('/proc').glob('[0-9]*'):
            try:
                env = (entry / 'environ').read_bytes().split(b'\0')
                command = (entry / 'cmdline').read_bytes().split(b'\0')
                owns_executable = command and command[0].startswith(os.fsencode(self.home) + b'/')
                if (f'CODEX_HOME={self.home}'.encode() in env or owns_executable) and any(
                        part.endswith(b'codex-plus-runtime') or part == b'codex_plus.background'
                        for part in command):
                    owned.add(int(entry.name))
            except (OSError, ProcessLookupError):
                pass
        for pid in owned:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for child, log in self.children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            log.seek(0)
            self.logs.append({'observer': child.pid, 'returncode': child.returncode, 'output': log.read()})
            log.close()
        self.supervisor_lock.close()
        self.server.close()
        if EVIDENCE:
            case = EVIDENCE / CURRENT_CASE
            case.mkdir(parents=True, exist_ok=True)
            (case / 'cli.json').write_text(json.dumps(self.logs, indent=2))
            (case / 'rpc.json').write_text(json.dumps(self.server.requests, indent=2))
            (case / 'state.json').write_text(json.dumps(self.state(), indent=2))
        self.temporary.cleanup()


EVIDENCE = None
CURRENT_CASE = ''


@unittest.skipUnless(RUNTIME, 'set CODEX_PLUS_TEST_RUNTIME or pass --runtime to run the Rust product gate')
class RuntimeAcceptance(unittest.TestCase):
    @contextmanager
    def fixture(self, fake=None, label=None):
        global CURRENT_CASE
        CURRENT_CASE = self.id().rsplit('.', 1)[-1] + (f'--{label}' if label else '')
        fixture = PrivateRuntime(fake)
        try:
            yield fixture
            self.assertFalse(fixture.server.errors)
        finally:
            fixture.close()

    def test_input_settle_retries_only_temporary_turn_history_unavailability(self):
        unsupported = {'code': -32601, 'message': 'list_turns is not supported yet'}
        origin, other = str(uuid.uuid4()), str(uuid.uuid4())
        cases = [
            ('recovers', {'error': unsupported}, 2, origin, True),
            ('recovers_late', {'error': unsupported}, 6, origin, True),
            ('empty_then_exact', {'result': {'data': [], 'nextCursor': None}}, 2, origin, True),
            ('previous_then_exact', {'result': {'data': [
                {'id': other, 'status': 'completed'}], 'nextCursor': None}}, 2, origin, True),
            ('persistent', {'error': unsupported}, 100, origin, False),
            ('wrong_code', {'error': dict(unsupported, code=-32603)}, 1, origin, False),
            ('other_error', {'error': dict(unsupported, message='permission denied')}, 1, origin, False),
            ('wrong_turn', {'error': unsupported}, 2, other, False),
            ('unknown_turn', None, 0, other, False),
            ('absent_turn', None, 0, None, False),
        ]
        for label, response, failures, known_turn, accepted in cases:
            with self.subTest(label=label), self.fixture(label=label) as f:
                f.env.pop('CODEX_THREAD_ID')
                f.fake.parent_turns = []
                result = subprocess.run([
                    str(f.runtime), 'ultracode', 'provenance', '--thread', PARENT,
                    '--load-kind', 'fresh', '--developer-instructions-stdin', '--json',
                ], env=f.env, input=(REPO / 'docs/ultracode-bootstrap.txt').read_text(),
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
                f.cli('ultracode', 'on', '--thread', PARENT, '--json')
                submission = str(uuid.uuid4())
                submitted = json.loads(f.cli('goal', 'input', '--thread', PARENT,
                    '--submission-id', submission, '--json').stdout)
                f.fake.parent_turns = ([{'id': known_turn, 'status': 'inProgress'}]
                                       if known_turn else [])
                f.fake.child_turns = [{'id': origin, 'status': 'inProgress'}]
                f.fake.entries = [{'turnId': known_turn, 'item': {
                    'type': 'userMessage', 'clientId': submission,
                }}]
                original_reply = f.server.reply
                reads = []

                def reply(method, params):
                    if method == 'thread/turns/list':
                        reads.append(dict(params))
                        if len(reads) <= failures:
                            return response
                    return original_reply(method, params)

                f.server.reply = reply
                before = len(f.server.requests)
                started = time.monotonic()
                result = f.cli('goal', 'input-settle', '--thread', PARENT,
                    '--submission-id', submission, '--turn', origin, '--accepted', '--json', ok=False)
                elapsed = time.monotonic() - started
                self.assertEqual(result.returncode == 0, accepted, (result.stdout, result.stderr))
                self.assertLess(elapsed, 3, 'readiness retries exceeded the short admission budget')
                self.assertTrue(all(p['threadId'] == PARENT for p in reads))
                if label == 'persistent':
                    self.assertGreater(len(reads), 1)
                    self.assertIn(unsupported['message'], result.stdout)
                elif accepted:
                    self.assertEqual(len(reads), failures + 1)
                    if label == 'recovers_late':
                        self.assertGreater(elapsed, .3, 'fixture must outlast the original readiness window')
                elif label in ('wrong_code', 'other_error'):
                    self.assertEqual(len(reads), 1)
                else:
                    self.assertGreaterEqual(len(reads), failures + 1)
                    self.assertIn('exact known root turn', result.stdout)
                self.assertTrue(all(r.get('method') in (
                    'initialize', 'initialized', 'thread/turns/list', 'thread/items/list',
                ) for r in f.server.requests[before:]), 'settlement replayed a mutation')
                status = json.loads(f.cli('goal', 'history', '--thread', PARENT, '--json').stdout)
                projection = json.loads((f.home / 'codex-plus/ultracode' / f'{PARENT}.json').read_text())
                self.assertEqual(status['root'], PARENT)
                self.assertEqual(status['input_epoch'], submitted['input_epoch'])
                if accepted:
                    self.assertEqual(result.stderr, '')
                    self.assertIsNone(status['pending_input'])
                    self.assertEqual(projection['effective']['submission'], submission)
                    self.assertEqual(projection['effective']['turn'], origin)
                    self.assertTrue(projection['effective']['enabled'])
                else:
                    self.assertEqual(status['pending_input']['submission_id'], submission)
                    self.assertEqual(status['pending_input']['state'], 'submitting')
                    self.assertIsNone(status['pending_input']['turn'])
                    self.assertIsNone(projection['effective'])

    def test_ultracode_item_readiness_preserves_input_and_reports_unavailable(self):
        unsupported = {'code': -32601, 'message': 'list_items is not supported yet'}
        empty = {'result': {'data': [], 'nextCursor': None}}
        cases = [
            ('recovers', {'error': unsupported}, 2, True, 3),
            ('persistent', {'error': unsupported}, 100, False, 4),
            ('wrong_code', {'error': dict(unsupported, code=-32603)}, 1, False, 1),
            ('other_error', {'error': dict(unsupported, message='permission denied')}, 1, False, 1),
            ('empty_once', empty, 1, True, 2),
            ('empty_twice', empty, 2, True, 3),
            ('empty_three', empty, 3, True, 4),
            ('empty_items', empty, 100, False, 4),
            ('mixed_recovers', [empty, {'error': unsupported}, empty], 3, True, 4),
            ('mixed_exhausted', [empty, {'error': unsupported}, empty, {'error': unsupported}], 4, False, 4),
            ('missing_client', None, 1, False, 1),
            ('unreserved', None, 1, False, 1),
            ('foreign_item', {'result': {'data': [{'turnId': str(uuid.uuid4()),
                'item': {'type': 'userMessage', 'clientId': str(uuid.uuid4())}}],
                'nextCursor': None}}, 1, False, 1),
        ]
        for label, response, failures, recovered, expected_reads in cases:
            with self.subTest(label=label), self.fixture(label=label) as f:
                f.env.pop('CODEX_THREAD_ID')
                f.fake.parent_turns = []
                result = subprocess.run([
                    str(f.runtime), 'ultracode', 'provenance', '--thread', PARENT,
                    '--load-kind', 'fresh', '--developer-instructions-stdin', '--json',
                ], env=f.env, input=(REPO / 'docs/ultracode-bootstrap.txt').read_text(),
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
                f.cli('ultracode', 'on', '--thread', PARENT, '--json')
                submission, turn = str(uuid.uuid4()), str(uuid.uuid4())
                f.cli('goal', 'input', '--thread', PARENT, '--submission-id', submission, '--json')
                f.fake.parent_turns = [{'id': turn, 'status': 'inProgress'}]
                f.fake.entries = [{'turnId': turn, 'item': {
                    'type': 'userMessage', 'clientId': submission,
                }}]
                original_reply = f.server.reply
                reads = []

                def reply(method, params):
                    if method == 'thread/items/list':
                        reads.append(dict(params))
                        if len(reads) <= failures:
                            if label in ('missing_client', 'unreserved'):
                                item = {'type': 'userMessage'}
                                if label == 'unreserved':
                                    item['clientId'] = str(uuid.uuid4())
                                return {'result': {'data': [{'turnId': turn, 'item': item}], 'nextCursor': None}}
                            return response[len(reads) - 1] if isinstance(response, list) else response
                    return original_reply(method, params)

                f.server.reply = reply
                before = len(f.server.requests)
                started = time.monotonic()
                result = f.cli('goal', 'input-settle', '--thread', PARENT,
                    '--submission-id', submission, '--turn', turn, '--accepted', '--json')
                self.assertLess(time.monotonic() - started, 3)
                self.assertIsNone(json.loads(result.stdout)['pending_input'])
                projection_path = f.home / 'codex-plus/ultracode' / f'{PARENT}.json'
                projection = json.loads(projection_path.read_text())
                self.assertTrue(all(p['threadId'] == PARENT and p['turnId'] == turn for p in reads))
                self.assertEqual(len(reads), expected_reads)
                if recovered:
                    self.assertEqual(result.stderr, '')
                    self.assertEqual(projection['availability'], 'available')
                    self.assertEqual(projection['effective']['submission'], submission)
                    self.assertEqual(projection['effective']['turn'], turn)
                else:
                    self.assertEqual(projection['availability'], 'unavailable')
                    self.assertTrue(projection['unavailable_reason'])
                    self.assertIsNone(projection['effective'])
                    self.assertEqual(projection['instructions'], '')
                    self.assertIn(submission, [p['submission'] for p in projection['pending']])
                    if label in ('persistent', 'empty_items'):
                        # A status read must not conceal the still-unresolved error.
                        status = json.loads(f.cli('ultracode', 'status', '--thread', PARENT, '--json').stdout)
                        self.assertEqual(status['availability'], 'unavailable')
                        reason = unsupported['message'] if label == 'persistent' else 'initial user submission is not yet visible'
                        self.assertIn(reason, status['unavailable_reason'])
                        self.assertEqual(len(reads), expected_reads * 2)
                self.assertTrue(all(r.get('method') in (
                    'initialize', 'initialized', 'thread/read', 'thread/turns/list', 'thread/items/list',
                ) for r in f.server.requests[before:]), 'mode confirmation replayed a mutation')
                f.server.reply = original_reply
                status = json.loads(f.cli('ultracode', 'status', '--thread', PARENT, '--json').stdout)
                self.assertEqual(status['availability'], 'available')
                self.assertNotIn('unavailable_reason', status)
                self.assertEqual(status['effective']['submission'], submission)
                self.assertEqual(status['effective']['turn'], turn)
                self.assertTrue(status['effective']['enabled'])
                self.assertEqual(status['pending'], [])
                # Steering a confirmed turn must retain its original mode without rereading items.
                f.cli('ultracode', 'off', '--thread', PARENT, '--json')
                steer = str(uuid.uuid4())
                f.cli('goal', 'input', '--thread', PARENT, '--submission-id', steer, '--json')
                reads.clear()
                f.server.reply = reply
                f.cli('goal', 'input-settle', '--thread', PARENT, '--submission-id', steer,
                    '--turn', turn, '--accepted', '--json')
                self.assertEqual(reads, [])
                projection = json.loads(projection_path.read_text())
                self.assertEqual(projection['availability'], 'available')
                self.assertEqual(projection['effective'], status['effective'])

    def test_exact_worker_process_ids_and_python_reopen_deduplicate(self):
        with self.fixture() as f:
            f.fake.running = [{'processId': '23091', 'itemId': 'command-item'}]
            result = f.park('--worker', CHILD, '--process', '23091')
            expected = {f'{PARENT}:worker:{CHILD}:work', f'{PARENT}:process:item:command-item'}
            self.assertEqual(set(result['armed']), expected)
            self.assertEqual({j['id'] for j in f.status()['jobs']}, expected)
            store = Store(f.home)
            try:
                again = register(store, f.fake, PARENT, [CHILD], ['23091'], 'Python reads Rust registration')
                self.assertEqual(again['state'], 'already_registered')
                self.assertEqual(len(store.status(PARENT)['parks']), 1)
            finally:
                store.close()
            bad = f.cli('park', '--worker', '/root/absent', '--reason', 'bad selection', ok=False)
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(len(f.state()['jobs']), 2)

    def test_completed_work_is_immediate_and_worker_new_turn_is_distinct(self):
        with self.fixture() as f:
            f.fake.complete()
            result = f.park('--worker', CHILD)
            self.assertEqual(result['state'], 'already_finished')
            self.assertFalse(f.state()['parks'])
            f.fake.child_turns = [{'id': 'next-work', 'status': 'inProgress'}, *f.fake.child_turns]
            self.assertEqual(f.park('--worker', '/root/work')['state'], 'armed')
            job = f.state()['jobs'][0]
            self.assertEqual(job['turn'], 'next-work')
            self.assertEqual(job['id'], f'{PARENT}:worker:{CHILD}:next-work')

    def test_completed_process_is_immediate_and_ambiguous_process_id_is_rejected(self):
        with self.fixture() as f:
            f.fake.entries = [{'turnId': 'old-execution', 'item': {
                'type': 'commandExecution', 'id': 'finished-command', 'processId': '42',
                'status': 'completed', 'exitCode': 0}}]
            self.assertEqual(f.park('--process', '42')['state'], 'already_finished')
            self.assertFalse(f.state()['jobs'])
            f.fake.entries.append({'turnId': 'other-execution', 'item': {
                'type': 'commandExecution', 'id': 'other-command', 'processId': '42',
                'status': 'completed', 'exitCode': 0}})
            result = f.cli('park', '--process', '42', '--reason', 'ambiguous process', ok=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(f.state()['parks'])

    def test_current_completed_process_ignores_long_older_history(self):
        with self.fixture(PagedHistoryFake()) as f:
            f.fake.entries = [{'turnId': 'old-history', 'item': {
                'type': 'userMessage', 'id': f'old-{index}'}} for index in range(3300)]
            f.fake.entries.append({'turnId': 'origin', 'item': {
                'type': 'commandExecution', 'id': 'finished-current', 'processId': '60257',
                'status': 'completed', 'exitCode': 0}})
            self.assertEqual(f.park('--process', '60257')['state'], 'already_finished')
            self.assertFalse(f.state()['parks'])
            self.assertFalse(f.state()['jobs'])
            self.assertFalse(f.state()['callbacks'])
            items = [params for method, params in f.fake.calls if method == 'thread/items/list']
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['turnId'], 'origin')
            self.assertEqual(f.fake.enqueues, 0)

    def test_current_process_match_scans_pages_and_wins_over_historical_id_reuse(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), self.fixture(PagedHistoryFake(), label=str(duplicate)) as f:
                command = {'turnId': 'origin', 'item': {
                    'type': 'commandExecution', 'id': 'finished-current', 'processId': '42',
                    'status': 'completed', 'exitCode': 0}}
                f.fake.entries = [{'turnId': 'old-turn', 'item': {
                    **command['item'], 'id': 'reused-old'}}]
                f.fake.entries.extend({'turnId': 'origin', 'item': {
                    'type': 'userMessage', 'id': f'prefix-{index}'}} for index in range(100))
                f.fake.entries.extend([command, {'turnId': 'origin', 'item': {
                    'type': 'userMessage', 'id': 'wrong-type', 'processId': '42'}},
                    {'turnId': 'origin', 'item': {**command['item'], 'id': 'wrong-process',
                                               'processId': '420'}}])
                f.fake.entries.extend({'turnId': 'origin', 'item': {
                    'type': 'userMessage', 'id': f'suffix-{index}'}} for index in range(100))
                if duplicate:
                    f.fake.entries.append({'turnId': 'origin', 'item': {
                        **command['item'], 'id': 'duplicate-current'}})
                    result = f.cli('park', '--process', '42', '--reason', 'duplicate current', ok=False)
                    self.assertNotEqual(result.returncode, 0)
                else:
                    self.assertEqual(f.park('--process', '42')['state'], 'already_finished')
                items = [params for method, params in f.fake.calls if method == 'thread/items/list']
                self.assertEqual(len(items), 3)
                self.assertTrue(all(params.get('turnId') == 'origin' for params in items))
                self.assertFalse(f.state()['parks'])
                self.assertFalse(f.state()['jobs'])
                self.assertFalse(f.state()['callbacks'])

    def test_process_batch_reuses_current_snapshot_and_falls_back_for_older_job(self):
        with self.fixture(PagedHistoryFake()) as f:
            f.fake.running = [{'processId': '11', 'itemId': 'running-command'}]
            f.fake.entries = [{'turnId': turn, 'item': {
                'type': 'commandExecution', 'id': item, 'processId': process,
                'status': 'completed', 'exitCode': 0}}
                for turn, item, process in [('origin', 'current-command', '42'),
                                            ('old-turn', 'older-command', '43')]]
            result = f.park('--process', '11', '--process', '42', '--process', '43')
            self.assertEqual(result['state'], 'armed')
            self.assertEqual({job['item']: job['status'] for job in f.state()['jobs']}, {
                'running-command': 'armed'})
            self.assertEqual({job['id']: job['status'] for job in result['already_finished']}, {
                f'{PARENT}:process:item:current-command': 'completed',
                f'{PARENT}:process:item:older-command': 'completed'})
            items = [params for method, params in f.fake.calls if method == 'thread/items/list']
            self.assertEqual([params.get('turnId') for params in items], ['origin', None])
            self.assertFalse(f.state()['callbacks'])
            self.assertEqual(f.fake.enqueues, 0)

    def test_old_process_beyond_history_bound_does_not_partially_register_batch(self):
        with self.fixture(PagedHistoryFake()) as f:
            f.fake.running = [{'processId': '11', 'itemId': 'running-command'}]
            f.fake.entries = [{'turnId': 'old-history', 'item': {
                'type': 'userMessage', 'id': f'old-{index}'}} for index in range(3300)]
            f.fake.entries.append({'turnId': 'old-turn', 'item': {
                'type': 'commandExecution', 'id': 'older-command', 'processId': '42',
                'status': 'completed', 'exitCode': 0}})
            result = f.cli('park', '--worker', CHILD, '--process', '11', '--process', '42',
                           '--reason', 'bounded old selection', ok=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('32 pages', result.stderr)
            items = [params for method, params in f.fake.calls if method == 'thread/items/list']
            self.assertEqual(sum(params.get('turnId') == 'origin' for params in items), 1)
            self.assertEqual(sum(not params.get('turnId') for params in items), 32)
            self.assertFalse(f.state()['parks'])
            self.assertFalse(f.state()['jobs'])
            self.assertFalse(f.state()['callbacks'])
            self.assertEqual(f.fake.enqueues, 0)

    def test_current_process_lookup_errors_do_not_accept_partial_or_unowned_evidence(self):
        command = {'turnId': 'origin', 'item': {
            'type': 'commandExecution', 'id': 'current-command', 'processId': '42',
            'status': 'completed', 'exitCode': 0}}
        faults = {
            'repeated-cursor': {'data': [], 'nextCursor': 'fault'},
            'malformed-data': {'data': {}},
            'malformed-cursor': {'data': [], 'nextCursor': 42},
            'transport': ConnectionError('current history transport failed'),
            'wrong-turn': {'data': [{**command, 'turnId': 'other-turn'}], 'nextCursor': None},
            'missing-turn': {'data': [{'item': command['item']}], 'nextCursor': None},
            'bounded-current': None,
        }
        for label, reply in faults.items():
            with self.subTest(fault=label), self.fixture(PagedHistoryFake(), label=label) as f:
                f.fake.running = [{'processId': '11', 'itemId': 'running-command'}]
                f.fake.entries = [command]
                if label == 'bounded-current':
                    f.fake.entries.extend({'turnId': 'origin', 'item': {
                        'type': 'userMessage', 'id': f'current-{index}'}} for index in range(3300))
                else:
                    f.fake.item_replies = {
                        ('origin', None): {'data': [command], 'nextCursor': 'fault'},
                        ('origin', 'fault'): reply,
                    }
                result = f.cli('park', '--process', '11', '--process', '42',
                               '--reason', 'unverified current selection', ok=False)
                self.assertNotEqual(result.returncode, 0)
                items = [params for method, params in f.fake.calls if method == 'thread/items/list']
                self.assertEqual(len(items), 32 if label == 'bounded-current' else 2)
                self.assertTrue(all(params.get('turnId') == 'origin' for params in items))
                self.assertFalse(f.state()['parks'])
                self.assertFalse(f.state()['jobs'])
                self.assertFalse(f.state()['callbacks'])
                self.assertEqual(f.fake.enqueues, 0)

    def test_live_delivery_waits_for_idle_and_preserves_python_dedup(self):
        with self.fixture() as f:
            f.park('--worker', CHILD)
            observer = f.observe()
            wait(f.claimed, 'Rust observer never claimed registered jobs')
            f.fake.complete()
            wait(lambda: f.state()['callbacks'], 'Rust observer did not batch completed worker')
            self.assertFalse(f.fake.starts, 'callback started while parent was active')
            f.fake.idle()
            wait(f.delivered, 'Rust callback was not delivered')
            observer.wait(timeout=5)
            self.assertEqual(observer.returncode, 0)
            callback = f.state()['callbacks'][0]
            self.assertIn('LIVE COMPLETION', callback['message'])
            self.assertEqual(len(f.fake.starts), 1)
            store = Store(f.home)
            try:
                Observer(store, PARENT, lambda: f.fake).step()
            finally:
                store.close()
            self.assertEqual(len(f.fake.starts), 1, 'Python reopening delivered Rust ledger redelivered')

    def test_python_frozen_uncertain_stock_callback_is_not_blindly_enqueued(self):
        with self.fixture() as f:
            store = Store(f.home)
            try:
                register(store, f.fake, PARENT, [CHILD], [], '0.2 frozen callback')
                f.fake.complete()
                Observer(store, PARENT, lambda: f.fake).step()
                frozen = store.status(PARENT)['callbacks'][0]
                frozen = store.claim_callback(frozen, native=False)
                store.dispatch_result(frozen, starting=False, acknowledged=False)
                before = store.status(PARENT)['callbacks'][0]
            finally:
                store.close()
            f.fake.idle()
            observer = f.observe()
            observer.wait(timeout=5)
            self.assertEqual(observer.returncode, 0)
            after = f.status()['callbacks'][0]
            self.assertEqual(after['state'], 'uncertain', 'stock ambiguity lost')
            self.assertEqual({k: before[k] for k in ('id', 'client', 'message', 'idempotent')},
                             {k: after[k] for k in ('id', 'client', 'message', 'idempotent')})
            self.assertEqual(f.fake.enqueues, 0, 'stock uncertain callback was blindly retried')
            self.assertEqual(f.status()['health']['state'], 'uncertain')

    def seed_old_uncertain_and_fresh(self, f, *, cancelling=False):
        store = Store(f.home)
        try:
            register(store, f.fake, PARENT, [CHILD], [], 'old frozen callback')
            f.fake.complete()
            Observer(store, PARENT, lambda: f.fake).step()
            callback = store.claim_callback(store.status(PARENT)['callbacks'][0], native=False)
            store.dispatch_result(callback)
            if cancelling:
                store.request_cancel(PARENT)
                store.reconcile_evidence(callback)
            frozen = store.status(PARENT)['callbacks'][0]
            f.fake.child_turns.insert(0, {'id': 'fresh', 'status': 'inProgress'})
            register(store, f.fake, PARENT, [CHILD], [], 'fresh callback')
        finally:
            store.close()
        f.fake.entries = [{'turnId': 'old-history', 'item': {
            'type': 'userMessage', 'id': f'history-{index}', 'clientId': f'unrelated-{index}'}}
            for index in range(3300)]
        f.fake.complete()
        f.fake.idle()
        return frozen

    def test_bounded_old_callback_history_does_not_starve_fresh_completion(self):
        for cancelling in (False, True):
            with self.subTest(cancelling=cancelling), self.fixture(PagedHistoryFake(), label=str(cancelling)) as f:
                frozen = self.seed_old_uncertain_and_fresh(f, cancelling=cancelling)
                observer = f.observe()
                wait(lambda: any(c['id'] != frozen['id'] and c['state'] == 'delivered'
                                 for c in f.state()['callbacks']),
                     'old bounded history blocked fresh callback')
                observer.wait(timeout=5)
                self.assertEqual(observer.returncode, 0)
                after = next(c for c in f.status()['callbacks'] if c['id'] == frozen['id'])
                self.assertEqual(after['state'], 'cancel_uncertain' if cancelling else 'uncertain')
                self.assertIn('32 pages', after['error'])
                self.assertEqual({k: frozen[k] for k in ('id', 'client', 'message', 'idempotent')},
                                 {k: after[k] for k in ('id', 'client', 'message', 'idempotent')})
                self.assertEqual(f.fake.enqueues, 1)
                self.assertEqual(len(f.fake.starts), 1)
                self.assertEqual(f.status()['health']['state'], 'uncertain')

    def test_history_transport_failure_still_blocks_dispatch(self):
        with self.fixture(PagedHistoryFake()) as f:
            frozen = self.seed_old_uncertain_and_fresh(f)
            f.fake.history_error = True
            f.observe()
            wait(lambda: any('Monitoring unavailable (RpcError)' in (p['error'] or '')
                             for p in f.state()['parks']), 'history RPC failure was swallowed')
            self.assertEqual(f.fake.enqueues, 0)
            self.assertFalse(f.fake.starts)
            after = next(c for c in f.state()['callbacks'] if c['id'] == frozen['id'])
            self.assertEqual(after['state'], 'uncertain')
            self.assertEqual(after['message'], frozen['message'])

    def test_legacy_cancelled_ready_callback_does_not_block_fresh_completion(self):
        with self.fixture() as f:
            store = Store(f.home)
            try:
                register(store, f.fake, PARENT, [CHILD], [], 'old poisoned callback')
                f.fake.complete()
                Observer(store, PARENT, lambda: f.fake).step()
                poisoned = store.status(PARENT)['callbacks'][0]
                store.request_cancel(PARENT)
                provenance = store.status(PARENT)['parks'][0]
                with store._db:
                    store._db.execute("UPDATE callbacks SET state='ready' WHERE id=?", (poisoned['id'],))
                f.fake.child_turns.insert(0, {'id': 'fresh', 'status': 'inProgress'})
                register(store, f.fake, PARENT, [CHILD], [], 'fresh callback')
            finally:
                store.close()
            f.fake.complete()
            f.fake.idle()
            observer = f.observe()
            wait(lambda: any(c['id'] != poisoned['id'] and c['state'] == 'delivered'
                             for c in f.state()['callbacks']), 'poisoned callback blocked fresh wake')
            observer.wait(timeout=5)
            self.assertEqual(observer.returncode, 0)
            state = f.status()
            self.assertEqual(next(c for c in state['callbacks'] if c['id'] == poisoned['id'])['state'], 'cancelled')
            self.assertEqual(state['parks'][0], provenance)
            self.assertEqual(f.fake.enqueues, 1)
            self.assertEqual(len(f.fake.starts), 1)

    def test_python_keyed_uncertain_callback_reconciles_with_same_identity(self):
        with self.fixture(NativeFake()) as f:
            store = Store(f.home)
            try:
                register(store, f.fake, PARENT, [CHILD], [], '0.2 keyed callback')
                f.fake.complete()
                Observer(store, PARENT, lambda: f.fake).step()
                frozen = store.claim_callback(store.status(PARENT)['callbacks'][0], native=True)
                store.dispatch_result(frozen, starting=False, acknowledged=False)
                before = store.status(PARENT)['callbacks'][0]
            finally:
                store.close()
            f.fake.idle()
            f.observe()
            wait(f.delivered, 'Rust did not reconcile 0.2 keyed callback')
            after = f.status()['callbacks'][0]
            self.assertEqual({k: before[k] for k in ('id', 'client', 'message', 'idempotent')},
                             {k: after[k] for k in ('id', 'client', 'message', 'idempotent')})
            self.assertEqual(f.fake.enqueues, 1)
            self.assertEqual(len(f.fake.starts), 1)
            add = [p for method, p in f.fake.calls if method == 'thread/queue/add']
            self.assertEqual(add[0]['idempotencyKey'], before['client'])

    def test_python_cancel_provenance_survives_rust_cancel_and_reopen(self):
        with self.fixture() as f:
            store = Store(f.home)
            try:
                register(store, f.fake, PARENT, [CHILD], [], '0.2 confirmed origin')
                store.claim_observation(PARENT, 'python-0.2-owner')
                store.request_cancel(PARENT, 'origin', evidence={
                    'status': 'interrupted', 'source': 'rollout', 'event': 'turn_aborted',
                    'timestamp': '2026-10-17T00:00:00Z'})
                before = store.status(PARENT)
            finally:
                store.close()
            self.assertEqual(f.status()['parks'][0]['cancel_evidence'], before['parks'][0]['cancel_evidence'])
            f.cli('park', '--cancel', '--parent', PARENT)
            after = f.state()
            for key in ('id', 'cancelled_at', 'cancel_kind', 'cancel_evidence'):
                self.assertEqual(after['parks'][0][key], before['parks'][0][key])
            self.assertEqual(after['parks'][0]['cancel_kind'], 'confirmed_origin')
            self.assertEqual(json.loads(after['parks'][0]['cancel_evidence'])['observer'], 'python-0.2-owner')
            f.fake.parent_status = 'notLoaded'
            f.observe().wait(timeout=5)
            self.assertFalse(any(method == 'thread/resume' for method, _ in f.fake.calls))

    def test_rust_cancel_deletes_only_python_owned_pending_queue_entry(self):
        with self.fixture() as f:
            store = Store(f.home)
            try:
                register(store, f.fake, PARENT, [CHILD], [], '0.2 pending callback')
                f.fake.complete()
                Observer(store, PARENT, lambda: f.fake).step()
                callback = store.claim_callback(store.status(PARENT)['callbacks'][0])
                queued = f.fake.call('thread/queue/add', {'threadId': PARENT,
                    'clientUserMessageId': callback['client'],
                    'input': [{'type': 'text', 'text': callback['message']}]})['queuedSubmission']
                store.dispatch_result(callback, queued=queued['id'])
            finally:
                store.close()
            foreign = {'id': 'foreign-entry', 'clientUserMessageId': 'user-owned'}
            f.fake.queue.append(foreign)
            f.cli('park', '--cancel', '--parent', PARENT)
            observer = f.observe()
            wait(lambda: f.state()['callbacks'][0]['state'] == 'cancelled', 'owned queue cancellation not reconciled')
            observer.wait(timeout=5)
            self.assertEqual(observer.returncode, 0)
            self.assertEqual(f.fake.queue, [foreign])
            self.assertFalse(f.fake.starts)
            store = Store(f.home)
            try:
                Observer(store, PARENT, lambda: f.fake).step()
            finally:
                store.close()
            self.assertEqual(f.fake.queue, [foreign])
            self.assertEqual(f.fake.enqueues, 1, 'cancelled Python callback was re-enqueued')

    def test_interrupted_projection_requires_positive_terminal_evidence(self):
        with self.fixture() as f:
            f.park('--worker', CHILD)
            f.observe()
            wait(f.claimed, 'observer did not claim')
            f.fake.parent_turns[0].update(status='interrupted', completedAt=None)
            f.fake.parent_status = 'idle'
            f.fake.complete()
            wait(lambda: f.state()['parks'][0]['origin_pending'], 'projection was treated as confirmed abort')
            self.assertEqual(f.state()['parks'][0]['state'], 'armed')
            self.assertFalse(f.fake.starts)
            sessions = f.home / 'sessions'
            sessions.mkdir()
            path = sessions / 'origin.jsonl'
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': PARENT}}) + '\n' +
                            json.dumps({'type': 'event_msg', 'timestamp': '2026-10-17T00:00:00Z',
                                        'payload': {'type': 'turn_aborted', 'turn_id': 'origin',
                                                    'reason': 'interrupted'}}) + '\n')
            f.fake.paths[PARENT] = str(path)
            wait(lambda: f.state()['parks'][0]['state'] == 'cancelled', 'durable abort did not cancel')
            park = f.state()['parks'][0]
            self.assertEqual(park['cancel_kind'], 'confirmed_origin')
            self.assertEqual(json.loads(park['cancel_evidence'])['source'], 'rollout')
            self.assertFalse(f.fake.starts)

    def test_generation_change_foreign_queue_prevents_unloaded_root_resume(self):
        with self.fixture() as f:
            store = Store(f.home)
            try:
                f.fake.generation = 'old-boot:123:4'
                register(store, f.fake, PARENT, [CHILD], [], '0.2 before daemon restart')
                store.claim_observation(PARENT, 'previous-observer')
            finally:
                store.close()
            f.fake.parent_status = 'notLoaded'
            f.fake.queue = [{'id': 'foreign', 'clientUserMessageId': 'user-owned'}]
            f.observe()
            wait(f.claimed, 'new observer failed to claim')
            wait(lambda: f.state()['parks'][0]['recovery_generation'], 'new daemon generation not observed')
            time.sleep(.1)
            self.assertFalse(any(method == 'thread/resume' for method, _ in f.fake.calls))
            self.assertEqual(f.fake.enqueues, 0)
            self.assertFalse(f.fake.starts)

    def test_restarted_observer_reports_completed_old_work_once(self):
        with self.fixture() as f:
            store = Store(f.home)
            try:
                register(store, f.fake, PARENT, [CHILD], [], '0.2 interrupted observer')
                store.claim_observation(PARENT, 'previous-observer')
            finally:
                store.close()
            f.fake.idle()
            f.fake.complete()
            f.observe()
            wait(f.delivered, 'restarted observer did not deliver report')
            callback = f.state()['callbacks'][0]
            self.assertIn('REPORT ONLY', callback['message'])
            self.assertNotIn('LIVE COMPLETION', callback['message'])
            self.assertEqual(f.state()['jobs'][0]['report'], 1)
            store = Store(f.home)
            try:
                Observer(store, PARENT, lambda: f.fake).step()
            finally:
                store.close()
            self.assertEqual(len(f.fake.starts), 1)

    def test_mixed_origin_completions_keep_separate_callbacks(self):
        with self.fixture() as f:
            f.park('--worker', CHILD)
            f.fake.parent_turns = [{'id': 'new-origin', 'status': 'inProgress'},
                                   {'id': 'origin', 'status': 'completed'}]
            f.fake.running = [{'processId': '9001', 'itemId': 'mixed-command'}]
            f.park('--process', '9001')
            f.observe()
            wait(lambda: all(j['observer_run'] for j in f.state()['jobs']), 'mixed jobs not claimed')
            f.fake.complete()
            f.fake.running.clear()
            f.fake.entries.append({'turnId': 'new-origin', 'item': {
                'type': 'commandExecution', 'id': 'mixed-command', 'processId': '9001',
                'status': 'completed', 'exitCode': 0}})
            f.fake.parent_turns[0]['status'] = 'completed'
            f.fake.parent_status = 'idle'
            wait(lambda: len(callbacks := f.state()['callbacks']) == 2
                 and all(c['state'] == 'delivered' for c in callbacks),
                 'different registration origins were merged')
            state = f.state()
            self.assertEqual(len({j['callback'] for j in state['jobs']}), 2)
            self.assertEqual(len(f.fake.starts), 2)
            self.assertTrue(all('LIVE COMPLETION' in c['message'] for c in state['callbacks']))

    def test_single_observer_owner_lock_then_takeover_reports_old_completion(self):
        with self.fixture() as f:
            f.park('--worker', CHILD)
            first = f.observe()
            wait(f.claimed, 'first observer did not claim ownership')
            owner = f.state()['jobs'][0]['observer_run']
            second = f.observe()
            time.sleep(.15)
            self.assertIsNone(second.poll(), 'second observer did not wait on owner lock')
            self.assertEqual(f.state()['jobs'][0]['observer_run'], owner)
            first.terminate()
            first.wait(timeout=3)
            wait(lambda: f.state()['jobs'][0]['observer_run'] != owner, 'lock not released after owner exit')
            f.fake.idle()
            f.fake.complete()
            wait(f.delivered, 'replacement observer did not deliver exactly once')
            self.assertEqual(len(f.fake.starts), 1)
            self.assertEqual(len(f.state()['callbacks']), 1)

    def test_generation_change_resumes_unloaded_registered_root_and_reports_loss(self):
        with self.fixture() as f:
            store = Store(f.home)
            try:
                f.fake.generation = 'old-boot:123:4'
                register(store, f.fake, PARENT, [CHILD], [], '0.2 registered root')
                store.claim_observation(PARENT, 'previous-observer')
            finally:
                store.close()
            f.fake.parent_status = 'notLoaded'
            f.fake.parent_turns[0]['status'] = 'completed'
            f.fake.child_turns.clear()
            f.observe()
            wait(f.delivered, 'recovered unloaded root did not receive loss report')
            self.assertEqual(f.state()['jobs'][0]['status'], 'lost')
            self.assertEqual(len([m for m, _ in f.fake.calls if m == 'thread/resume']), 1)
            self.assertIn('REPORT ONLY', f.state()['callbacks'][0]['message'])
            self.assertEqual(len(f.fake.starts), 1)

    def test_lost_queue_add_ack_reconciles_positive_queue_without_second_add(self):
        for factory in (Fake, NativeFake):
            with self.subTest(capability=factory.__name__), self.fixture(factory(), label=factory.__name__) as f:
                f.park('--worker', CHILD)
                f.server.drop_after_method = 'thread/queue/add'
                observer = f.observe()
                wait(f.claimed, 'observer did not claim before transport loss')
                f.fake.complete()
                f.fake.idle()
                self.assertTrue(f.server.lost_ack.wait(8), 'queue/add response was not dropped')
                wait(f.delivered, 'positive queue/receipt evidence did not reconcile lost add acknowledgment', timeout=15)
                observer.wait(timeout=5)
                self.assertEqual(observer.returncode, 0)
                self.assertEqual(f.fake.enqueues, 1)
                self.assertEqual(len(f.fake.starts), 1)
                self.assertEqual(len([r for r in f.server.requests if r['method'] == 'thread/queue/add']), 1)
                callback = f.state()['callbacks'][0]
                self.assertEqual(f.fake.entries[0]['item']['clientId'], callback['client'])

    def test_lost_queue_start_ack_reconciles_positive_history_without_second_start(self):
        for factory in (Fake, NativeFake):
            with self.subTest(capability=factory.__name__), self.fixture(factory(), label=factory.__name__) as f:
                f.park('--worker', CHILD)
                f.server.drop_after_method = 'thread/queue/start'
                observer = f.observe()
                wait(f.claimed, 'observer did not claim before transport loss')
                f.fake.complete()
                f.fake.idle()
                self.assertTrue(f.server.lost_ack.wait(8), 'queue/start response was not dropped')
                wait(f.delivered, 'consumed history/receipt did not settle lost start acknowledgment', timeout=15)
                observer.wait(timeout=5)
                self.assertEqual(observer.returncode, 0)
                self.assertEqual(f.fake.enqueues, 1)
                self.assertEqual(len(f.fake.starts), 1)
                self.assertEqual(len([r for r in f.server.requests if r['method'] == 'thread/queue/start']), 1)
                self.assertEqual(f.fake.entries[0]['item']['clientId'], f.state()['callbacks'][0]['client'])

    def test_process_death_after_freeze_before_add_mutation_does_not_blindly_retry(self):
        with self.fixture() as f:
            f.park('--worker', CHILD)
            f.server.hold_before_method = 'thread/queue/add'
            f.server.discard_held = True
            observer = f.observe()
            wait(f.claimed, 'observer did not claim')
            f.fake.complete()
            f.fake.idle()
            self.assertTrue(f.server.method_entered.wait(8), 'observer did not reach frozen before-add boundary')
            before = f.state()['callbacks'][0]
            self.assertEqual(before['state'], 'in_flight')
            observer.kill()
            observer.wait(timeout=3)
            f.server.release_method.set()
            recovered = f.observe()
            recovered.wait(timeout=6)
            self.assertEqual(recovered.returncode, 0)
            after = f.state()['callbacks'][0]
            self.assertEqual(after['state'], 'uncertain')
            self.assertEqual({k: before[k] for k in ('id', 'client', 'message', 'idempotent')},
                             {k: after[k] for k in ('id', 'client', 'message', 'idempotent')})
            self.assertEqual(f.fake.enqueues, 0)
            self.assertFalse(f.fake.starts)

    def test_keyed_downgrade_waits_for_capability_but_accepts_positive_consumed_history(self):
        for consumed in (False, True):
            with self.subTest(consumed=consumed), self.fixture(NativeFake(), label=str(consumed)) as f:
                store = Store(f.home)
                try:
                    register(store, f.fake, PARENT, [CHILD], [], '0.2 keyed downgrade')
                    f.fake.complete()
                    Observer(store, PARENT, lambda: f.fake).step()
                    callback = store.claim_callback(store.status(PARENT)['callbacks'][0], native=True)
                    store.dispatch_result(callback)
                    before = store.status(PARENT)['callbacks'][0]
                finally:
                    store.close()
                f.fake.initialize_result = {}
                f.fake.idle()
                if consumed:
                    f.fake.entries.append({'turnId': 'historic-callback', 'item': {
                        'type': 'userMessage', 'id': 'historic-message', 'clientId': before['client']}})
                observer = f.observe()
                if consumed:
                    wait(f.delivered, 'stock history did not positively reconcile keyed callback')
                    observer.wait(timeout=5)
                    self.assertEqual(observer.returncode, 0)
                else:
                    wait(lambda: f.state()['callbacks'][0]['error'] ==
                         'Keyed callback waits for native queue idempotency support.',
                         'keyed downgrade did not expose capability uncertainty')
                    self.assertEqual(f.state()['callbacks'][0]['state'], 'uncertain')
                after = f.state()['callbacks'][0]
                self.assertEqual({k: before[k] for k in ('id', 'client', 'message', 'idempotent')},
                                 {k: after[k] for k in ('id', 'client', 'message', 'idempotent')})
                self.assertEqual(f.fake.enqueues, 0)
                self.assertFalse(f.fake.starts)
                self.assertFalse(any(m.startswith('thread/queue/receipt') for m, _ in f.fake.calls))

    def test_supervisor_preserves_inherited_final_attempt_budget_on_launch_failure(self):
        with self.fixture() as f:
            store = Store(f.home)
            try:
                register(store, f.fake, PARENT, [CHILD], [], '0.2 inherited supervisor budget')
            finally:
                store.close()
            # The supervisor now exits if its role is owned; test the inherited
            # final attempt using a loaded copy and a missing observer executable.
            supervisor_binary = f.runtime.with_name('supervisor-budget-fixture')
            shutil.copy2(f.runtime, supervisor_binary)
            f.runtime.unlink()
            f.supervisor_lock.close()
            log = (f.home / 'supervisor-budget.log').open('w+')
            supervisor = subprocess.Popen([str(supervisor_binary), 'supervise', '--parent', PARENT],
                cwd=REPO, env=dict(f.env, CODEX_PLUS_SUPERVISOR_FAILURES='5'),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            f.children.append((supervisor, log))
            supervisor.wait(timeout=6)
            self.assertEqual(supervisor.returncode, 0)
            state = f.state()
            self.assertEqual(state['parks'][0]['error'],
                             'Supervisor restart limit reached; observer recovery needs attention.')
            self.assertEqual(state['health']['state'], 'error')
            self.assertEqual(state['jobs'][0]['status'], 'armed')
            self.assertFalse(state['callbacks'])
            self.assertEqual(f.fake.enqueues, 0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--evidence', type=Path)
    args, remaining = parser.parse_known_args()
    RUNTIME = args.runtime.resolve(strict=True)
    EVIDENCE = args.evidence.resolve() if args.evidence else None
    # The direct invocation is an explicit gate, so do not permit an all-skipped run.
    RuntimeAcceptance.__unittest_skip__ = False
    RuntimeAcceptance.__unittest_skip_why__ = ''
    unittest.main(argv=[sys.argv[0], *remaining])
