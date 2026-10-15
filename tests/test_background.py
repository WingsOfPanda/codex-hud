"""Owned state and fake RPC lifecycle checks; no production queue writes."""
import fcntl
import json
import multiprocessing
import os
import socket
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import time

from codex_plus import background
from codex_plus.background import HistoryLimitError, Observer, Store, pages
from codex_plus.rpc import Client, peer_generation

PARENT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'


class Fake:
    def __init__(self):
        self.generation = 'boot:100:1'
        self.parent_status = 'active'
        self.parent_turns = [{'id': 'origin', 'status': 'inProgress'}]
        self.child_turns = [{'id': 'work', 'status': 'inProgress'}]
        self.child_runtime_status = 'notLoaded'
        self.children = [{'id': CHILD, 'parentThreadId': PARENT,
                          'source': {'subAgent': {'thread_spawn': {'agent_path': '/root/work'}}}}]
        self.entries = []
        self.running = []
        self.queue = []
        self.enqueues = 0
        self.starts = []
        self.calls = []
        self.fail_add = False
        self.consume_add = False
        self.fail_start = False
        self.fail_read = False
        self.paths = {}

    def close(self):
        pass

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if self.fail_read and method not in ('thread/queue/add', 'thread/queue/start'):
            raise ConnectionError('disconnected')
        if method == 'thread/read':
            if params['threadId'] == CHILD:
                return {'thread': {'id': CHILD, 'path': self.paths.get(CHILD), 'status': {'type': self.child_runtime_status}}}
            return {'thread': {'id': PARENT, 'path': self.paths.get(PARENT), 'source': 'cli', 'status': {'type': self.parent_status}}}
        if method == 'thread/resume':
            self.parent_status = 'idle'
            return {'thread': {'id': PARENT, 'source': 'cli', 'status': {'type': 'idle'}}}
        if method == 'thread/list':
            return {'data': self.children, 'nextCursor': None}
        if method == 'thread/turns/list':
            turns = self.child_turns if params['threadId'] == CHILD else self.parent_turns
            return {'data': turns[:params.get('limit', 100)], 'nextCursor': None}
        if method == 'thread/items/list':
            return {'data': [e for e in self.entries if not params.get('turnId') or e['turnId'] == params['turnId']], 'nextCursor': None}
        if method == 'thread/backgroundTerminals/list':
            return {'data': self.running, 'nextCursor': None}
        if method == 'thread/queue/list':
            return {'data': list(self.queue), 'nextCursor': None}
        if method == 'thread/queue/add':
            self.enqueues += 1
            q = {'id': str(uuid.uuid4()), 'clientUserMessageId': params['clientUserMessageId']}
            self.queue.append(q)
            if self.consume_add:
                self.consume(q)
            if self.fail_add:
                raise TimeoutError('ack lost')
            return {'queuedSubmission': q}
        if method == 'thread/queue/start':
            if self.parent_status != 'idle':
                raise ValueError('NotIdle')
            q = next(q for q in self.queue if q['id'] == params['queuedSubmissionId'])
            self.starts.append(q['id'])
            self.consume(q)
            if self.fail_start:
                raise TimeoutError('start ack lost')
            return {'turn': {'id': 'callback'}}
        if method == 'thread/queue/delete':
            self.queue = [q for q in self.queue if q['id'] != params['queuedSubmissionId']]
            return {'deleted': True}
        raise AssertionError(method)

    def consume(self, q):
        self.queue.remove(q)
        self.entries.append({'turnId': 'callback', 'item': {'type': 'userMessage', 'id': 'message', 'clientId': q['clientUserMessageId']}})

    def idle(self):
        self.parent_status = 'idle'
        self.parent_turns[0]['status'] = 'completed'

    def complete(self, status='completed'):
        self.child_turns[0]['status'] = status
        if status == 'interrupted':
            self.child_turns[0]['completedAt'] = 123


class NativeFake(Fake):
    def __init__(self):
        super().__init__()
        self.initialize_result = {'queueIdempotency': True}
        self.receipts = {}

    def call(self, method, params):
        if method not in ('thread/queue/add', 'thread/queue/receipt', 'thread/queue/receipt/cancel'):
            return super().call(method, params)
        self.calls.append((method, dict(params)))
        key = params.get('idempotencyKey')
        if method == 'thread/queue/add':
            if key is None:
                return super().call(method, params)
            assert key == params['clientUserMessageId']
            if key not in self.receipts:
                self.enqueues += 1
                queued = {'id': str(uuid.uuid4()), 'clientUserMessageId': key}
                self.queue.append(queued)
                self.receipts[key] = {'queuedSubmission': queued, 'receiptState': 'pending'}
                if self.consume_add:
                    self.consume(queued)
            receipt = self.receipts[key]
            if receipt['queuedSubmission'] is None:
                raise ValueError('idempotencyKey has been cancelled')
            if self.fail_add:
                raise TimeoutError('ack lost')
            return dict(receipt)
        if method == 'thread/queue/receipt/cancel':
            if key not in self.receipts:
                self.receipts[key] = {'queuedSubmission': None, 'receiptState': 'cancelled'}
            elif self.receipts[key]['receiptState'] == 'pending':
                self.queue = [q for q in self.queue if q['id'] != self.receipts[key]['queuedSubmission']['id']]
                self.receipts[key]['receiptState'] = 'cancelled'
        return {'receipt': self.receipts.get(key)}

    def consume(self, queued):
        super().consume(queued)
        receipt = self.receipts.get(queued['clientUserMessageId'])
        if receipt:
            receipt['receiptState'] = 'consumed'


def contender(home, gate, output):
    store = Store(home)
    fake = Fake()
    fake.idle()
    fake.complete()
    observer = Observer(store, PARENT, lambda: fake)
    def fault(point):
        if point == 'after_start':
            with open(output, 'a') as f:
                f.write('started\n')
    observer.fault = fault
    gate.wait(5)
    observer.run()
    store.close()


def exiting_owner(home, ready, release):
    directory = Store(home).directory
    with (directory / f'{PARENT}.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready.set()
        release.wait(5)


def migrate_store(home, gate):
    gate.wait(5)
    store = Store(home)
    store.close()


class Transport(unittest.TestCase):
    def test_linux_peer_generation_matches_process_and_changes_on_pid_reuse(self):
        first, second = socket.socketpair()
        try:
            generation = peer_generation(first)
            self.assertEqual(generation.split(':')[1], str(os.getpid()))
            self.assertEqual(generation, peer_generation(second))
            with patch('codex_plus.rpc.Path.read_text', side_effect=[
                    'another-boot', '1 (name with ) delimiters) S ' + '0 ' * 18 + '999']):
                self.assertEqual(peer_generation(first), f'another-boot:{os.getpid()}:999')
        finally:
            first.close()
            second.close()

    def client(self, wire):
        class Socket:
            def recv(self, size):
                data, self.wire = self.wire[:size], self.wire[size:]
                return data
            def sendall(self, data):
                self.sent.append(data)
            def settimeout(self, value):
                pass
        client = Client.__new__(Client)
        client.sock = Socket()
        client.sock.wire, client.sock.sent = wire, []
        client.buffer = b''
        client.deadline = time.monotonic() + 1
        return client

    def test_fragmented_json_with_interleaved_ping(self):
        client = self.client(b'\x01\x05{"id"' + b'\x89\x01p' + b'\x80\x03:1}')
        self.assertEqual(client.receive(), {'id': 1})
        self.assertEqual(client.sock.sent[0][0], 0x8a)

    def test_invalid_continuation_extension_and_control_frames(self):
        for wire in (b'\x80\x02{}', b'\xc1\x02{}', b'\x09\x01p'):
            with self.subTest(wire=wire), self.assertRaises(ValueError):
                self.client(wire).receive()

    def test_whole_rpc_read_deadline(self):
        client = self.client(b'')
        client.deadline = 0
        with self.assertRaises(TimeoutError):
            client.read_socket()


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.fake = Fake()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def register(self):
        return background.register(self.store, self.fake, PARENT, ['/root/work'], [], 'finish selected task')

    def callbacks(self):
        return self.store.status(PARENT)['callbacks']

    def payload(self):
        return next(params['input'][0]['text'] for method, params in self.fake.calls
                    if method == 'thread/queue/add')

    def health_file(self, parent=PARENT):
        return self.store.directory / f'{parent}.status.json'

    def terminal_rollout(self, thread, turn_id, event, **extra):
        path = Path(self.temp.name) / 'sessions' / f'{thread}.jsonl'
        path.parent.mkdir(exist_ok=True)
        rows = [{'type': 'session_meta', 'payload': {'id': thread}},
                {'type': 'event_msg', 'timestamp': '2026-10-15T01:02:03Z',
                 'payload': {'type': event, 'turn_id': turn_id, **extra}}]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.fake.paths[thread] = str(path)
        return path

    def test_ledger_claim_rechecks_cancellation_before_freezing(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        callback = self.callbacks()[0]
        self.store.request_cancel(PARENT)
        self.assertIsNone(self.store.claim_callback(callback, native=True))
        current = self.callbacks()[0]
        self.assertEqual(current['state'], 'cancelled')
        self.assertIsNone(current['message'])
        self.assertEqual(current['idempotent'], 0)

    def test_ledger_claim_rechecks_cancellation_before_start(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        callback = self.store.claim_callback(self.callbacks()[0])
        self.store.dispatch_result(callback, queued='owned-queue')
        pending = self.callbacks()[0]
        self.store.request_cancel(PARENT)
        self.assertIsNone(self.store.claim_callback(pending, starting=True))
        self.assertEqual(self.callbacks()[0]['state'], 'cancel_requested')

    def test_ledger_native_recovery_reuses_frozen_callback_identity_and_payload(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        original = dict(self.store.claim_callback(self.callbacks()[0], native=True))
        self.assertIn('LIVE COMPLETION', original['message'])
        self.assertIsNone(self.store.claim_callback(original, native=True))
        self.store.claim_observation(PARENT, 'replacement-observer', recovering=True)
        uncertain = self.callbacks()[0]
        self.store.reconcile_evidence(uncertain, receipt_absent=True)
        recovered = self.store.claim_callback(self.callbacks()[0], native=True)
        self.assertEqual((recovered['id'], recovered['client'], recovered['message']),
                         (original['id'], original['client'], original['message']))

    def test_ledger_stock_absence_cannot_authorize_enqueue_retry(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        original = dict(self.store.claim_callback(self.callbacks()[0]))
        self.store.claim_observation(PARENT, 'replacement-observer', recovering=True)
        self.store.reconcile_evidence(self.callbacks()[0], receipt_absent=True)
        current = self.callbacks()[0]
        self.assertEqual(current['state'], 'uncertain')
        self.assertEqual(current['message'], original['message'])
        self.assertEqual(self.store.next_callback(PARENT), (None, False))
        self.assertFalse(self.store.finish_cycle(PARENT))
        self.assertEqual(self.store.health(PARENT)['state'], 'uncertain')

    def test_ledger_keyed_start_ack_requires_receipt_to_deliver(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        callback = self.store.claim_callback(self.callbacks()[0], native=True)
        self.store.dispatch_result(callback, queued='owned-queue')
        pending = self.callbacks()[0]
        self.store.claim_callback(pending, starting=True, native=True)
        self.store.dispatch_result(pending, starting=True, acknowledged=True)
        self.assertEqual(self.callbacks()[0]['state'], 'starting')
        self.store.record_receipt(pending, 'consumed', 'owned-queue')
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')

    def test_unsupported_ready_keyed_callback_blocks_pending_stock_start(self):
        self.register()
        self.fake.complete()
        self.observer.poll_jobs()
        self.observer.batch()
        stock = self.store.claim_callback(self.callbacks()[0])
        self.store.dispatch_result(stock, queued='stock-queue')
        self.fake.queue = [{'id': 'stock-queue', 'clientUserMessageId': stock['client']}]
        self.fake.child_turns = [{'id': 'second-work', 'status': 'inProgress'}]
        self.register()
        self.fake.complete()
        self.observer.poll_jobs()
        self.observer.batch()
        ready, _ = self.store.next_callback(PARENT, native=True)
        keyed = self.store.claim_callback(ready, native=True)
        self.store.claim_observation(PARENT, 'replacement-observer', recovering=True)
        self.store.reconcile_evidence(keyed, receipt_absent=True)
        self.fake.idle()
        self.observer.deliver()
        callbacks = {row['id']: row for row in self.callbacks()}
        self.assertEqual(callbacks[stock['id']]['state'], 'pending')
        self.assertEqual(callbacks[keyed['id']]['state'], 'ready')
        self.assertEqual(callbacks[keyed['id']]['error'],
                         'Keyed callback waits for native queue idempotency support.')
        self.assertEqual(self.fake.starts, [])
        self.assertEqual(self.fake.enqueues, 0)

    def test_new_completion_is_short_live_with_result_lookup(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.observer.step()
        text = self.payload()
        self.assertIn('LIVE COMPLETION', text)
        self.assertIn('worker /root/work status=completed', text)
        self.assertIn(f'codex-plus park --status --parent {PARENT}', text)
        self.assertIn(self.callbacks()[0]['id'], text)
        self.assertNotIn('finish selected task', text)
        self.assertLess(len(text), 1000)

    def test_observer_restart_terminal_is_report_without_old_authorization(self):
        self.register()
        self.observer.step()
        self.fake.complete()
        self.fake.idle()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        self.assertIn('REPORT ONLY', self.payload())
        self.assertIn('Do not resume earlier actions', self.payload())
        self.assertNotIn('finish selected task', self.payload())

    def test_restart_running_observation_then_future_completion_is_live(self):
        self.register()
        self.observer.step()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        self.fake.complete()
        self.fake.idle()
        self.observer.step()
        self.assertIn('LIVE COMPLETION', self.payload())

    def test_report_recovery_survives_failed_observation_and_another_restart(self):
        self.register()
        self.observer.step()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.fake.fail_read = True
        with self.assertRaises(ConnectionError):
            self.observer.step()
        self.fake.fail_read = False
        self.fake.complete()
        self.fake.idle()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        self.assertIn('REPORT ONLY', self.payload())

    def test_new_registration_during_owner_run_stays_live(self):
        self.register()
        self.observer.step()
        self.fake.child_turns = [{'id': 'fresh', 'status': 'inProgress'}]
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.observer.step()
        self.assertIn('LIVE COMPLETION', self.payload())

    def test_mixed_recovered_and_fresh_completions_keep_separate_deliveries(self):
        self.register()
        self.observer.step()
        self.fake.running = [{'itemId': 'new-command', 'processId': '9'}]
        background.register(self.store, self.fake, PARENT, [], ['9'], 'new work')
        self.fake.complete()
        self.fake.running = []
        self.fake.entries = [{'turnId': 'origin', 'item': {'type': 'commandExecution',
                             'id': 'new-command', 'processId': '9', 'status': 'completed', 'exitCode': 0}}]
        self.fake.idle()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertTrue(self.observer.step())
        self.assertFalse(self.observer.step())
        payloads = [params['input'][0]['text'] for method, params in self.fake.calls
                    if method == 'thread/queue/add']
        self.assertEqual(len(payloads), 2)
        self.assertIn('REPORT ONLY', payloads[0])
        self.assertIn('LIVE COMPLETION', payloads[1])

    def test_generation_running_process_can_later_complete_live(self):
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        background.register(self.store, self.fake, PARENT, [], ['9'], 'process')
        self.fake.generation = 'boot:200:2'
        self.observer.step()
        self.fake.running = []
        self.fake.entries = [{'turnId': 'origin', 'item': {'type': 'commandExecution',
                             'id': 'held', 'processId': '9', 'status': 'completed', 'exitCode': 2}}]
        self.fake.idle()
        self.observer.step()
        self.assertIn('LIVE COMPLETION', self.payload())
        self.assertIn('exit=2', self.payload())

    def test_generation_stale_process_history_does_not_prove_running(self):
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        background.register(self.store, self.fake, PARENT, [], ['9'], 'process')
        self.fake.generation = 'boot:200:2'
        self.fake.running = []
        self.fake.entries = [{'turnId': 'origin', 'item': {'type': 'commandExecution',
                             'id': 'held', 'processId': '9', 'status': 'inProgress', 'exitCode': None}}]
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'lost')
        self.assertIn('REPORT ONLY', self.payload())
        self.assertIn('outcome=UNKNOWN', self.payload())

    def test_generation_active_new_worker_turn_does_not_revive_old_turn(self):
        self.register()
        self.fake.child_runtime_status = 'active'
        self.fake.child_turns.insert(0, {'id': 'new-turn', 'status': 'inProgress'})
        self.fake.generation = 'boot:200:2'
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'lost')
        self.assertIn('REPORT ONLY', self.payload())

    def test_restart_ready_unfrozen_backlog_is_report(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        self.assertIsNone(self.callbacks()[0]['message'])
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.fake.idle()
        self.observer.step()
        self.assertIn('REPORT ONLY', self.payload())

    def test_generation_restart_running_worker_can_later_complete_live(self):
        self.register()
        self.fake.child_runtime_status = 'active'
        self.fake.generation = 'boot:200:2'
        self.observer.step()
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'armed')
        self.fake.complete()
        self.fake.idle()
        self.observer.step()
        self.assertIn('LIVE COMPLETION', self.payload())

    def test_registration_publishes_private_parent_scoped_health(self):
        self.register()
        view = json.loads(self.health_file().read_text())
        self.assertEqual(set(view), {'version', 'parent', 'updated_at', 'state', 'jobs', 'uncertain', 'reports'})
        self.assertEqual((view['version'], view['parent'], view['state'], view['jobs']), (1, PARENT, 'waiting', 1))
        self.assertEqual(os.stat(self.health_file()).st_mode & 0o777, 0o600)
        self.assertLess(self.health_file().stat().st_size, 1024)
        self.assertNotIn('finish selected task', self.health_file().read_text())
        other = '33333333-3333-4333-8333-333333333333'
        self.assertEqual(self.store.status(other)['health']['state'], 'idle')
        self.assertFalse(self.health_file(other).exists())

    def test_status_read_does_not_refresh_dead_observer_heartbeat(self):
        with patch('codex_plus.background.time.time', return_value=100):
            self.register()
        before = self.health_file().read_bytes()
        with patch('codex_plus.background.time.time', return_value=200):
            view = self.store.status(PARENT)['health']
        self.assertEqual(view['updated_at'], 100)
        self.assertEqual(self.health_file().read_bytes(), before)

    def test_health_completed_live_job_waiting_for_parent_has_zero_running_jobs(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        view = json.loads(self.health_file().read_text())
        self.assertEqual((view['state'], view['jobs'], view['reports']), ('waiting', 0, 0))
        self.fake.idle()
        self.observer.step()
        self.assertEqual(json.loads(self.health_file().read_text())['state'], 'idle')

    def test_health_read_rejects_symlink_and_fifo_without_refreshing(self):
        self.health_file().symlink_to(self.store.directory / 'background.sqlite')
        self.assertEqual(self.store.health(PARENT)['updated_at'], 0)
        self.health_file().unlink()
        os.mkfifo(self.health_file())
        self.assertEqual(self.store.health(PARENT)['updated_at'], 0)

    def test_atomic_publish_failure_preserves_previous_snapshot(self):
        self.register()
        before = self.health_file().read_bytes()
        with patch('codex_plus.background.os.replace', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.store.publish(PARENT)
        self.assertEqual(self.health_file().read_bytes(), before)
        self.assertEqual(list(self.store.directory.glob('*.tmp')), [])

    def test_legacy_job_columns_migrate_old_work_to_report(self):
        self.register()
        with sqlite3.connect(self.store.directory / 'background.sqlite') as db:
            for column in ('observer_run', 'recovery_pending', 'report', 'label'):
                db.execute(f'ALTER TABLE jobs DROP COLUMN {column}')
        self.store.close()
        self.store = Store(self.temp.name)
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.fake.complete()
        self.fake.idle()
        self.observer.step()
        self.assertIn('REPORT ONLY', self.payload())

    def test_legacy_frozen_callback_identity_and_text_are_preserved(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        callback = self.callbacks()[0]
        with sqlite3.connect(self.store.directory / 'background.sqlite') as db:
            db.execute('UPDATE callbacks SET message=? WHERE id=?', ('legacy frozen payload', callback['id']))
            for column in ('observer_run', 'recovery_pending', 'report', 'label'):
                db.execute(f'ALTER TABLE jobs DROP COLUMN {column}')
        self.store.close()
        self.store = Store(self.temp.name)
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.fake.generation = 'boot:200:2'
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.payload(), 'legacy frozen payload')
        self.assertEqual(self.callbacks()[0]['id'], callback['id'])
        self.assertEqual(self.callbacks()[0]['client'], callback['client'])

    def test_long_rpc_poll_refreshes_observer_heartbeat(self):
        self.register()
        with patch('codex_plus.background.time.monotonic', return_value=100):
            self.observer.call('thread/read', {'threadId': PARENT})
        with patch('codex_plus.background.time.monotonic', return_value=106), patch.object(self.store, 'publish', wraps=self.store.publish) as publish:
            self.observer.call('thread/read', {'threadId': PARENT})
        publish.assert_called_once_with(PARENT)

    def test_health_uncertain_stays_visible_after_observer_stops(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.observer.fault = lambda point: (_ for _ in ()).throw(SystemExit(79)) if point == 'before_enqueue' else None
        with self.assertRaises(SystemExit):
            self.observer.step()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        view = json.loads(self.health_file().read_text())
        self.assertEqual((view['state'], view['uncertain']), ('uncertain', 1))

    def test_report_health_until_delivered_then_idle(self):
        self.register()
        self.observer.step()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.fake.complete()
        self.observer.step()
        view = json.loads(self.health_file().read_text())
        self.assertEqual((view['state'], view['reports']), ('report', 1))
        self.fake.idle()
        self.observer.step()
        self.assertEqual(json.loads(self.health_file().read_text())['state'], 'idle')

    def test_supervisor_health_error_and_explicit_cancel_clear_warning(self):
        self.register()
        with patch('codex_plus.background.subprocess.Popen') as popen, patch('codex_plus.background.time.sleep'):
            popen.return_value.wait.return_value = 79
            background.supervise(self.store, PARENT, self.temp.name)
        self.assertEqual(json.loads(self.health_file().read_text())['state'], 'error')
        self.store.request_cancel(PARENT)
        self.assertEqual(json.loads(self.health_file().read_text())['state'], 'idle')

    def test_expired_error_can_be_dismissed_without_deleting_ledger(self):
        self.register()
        self.store.expire_registration(self.store.status(PARENT)['parks'][0]['id'])
        self.store.request_cancel(PARENT)
        self.assertEqual(self.store.status(PARENT)['health']['state'], 'idle')
        self.assertEqual(len(self.store.status(PARENT)['jobs']), 1)
        with self.assertRaisesRegex(ValueError, 'cannot be rearmed'):
            self.register()

    def test_run_expiration_warning_remains_visible_after_clean_exit(self):
        with patch('codex_plus.ledger.time.time', return_value=0):
            self.register()
        self.fake.idle()
        self.observer.run()
        self.assertEqual(json.loads(self.health_file().read_text())['state'], 'error')
        self.assertEqual(self.fake.enqueues, 0)
        self.store.request_cancel(PARENT)
        self.assertEqual(json.loads(self.health_file().read_text())['state'], 'idle')

    def test_observer_backoff_heartbeats_are_bounded_and_errors_sanitized(self):
        self.register()
        sleeps, states = [], []
        def sleep(delay):
            sleeps.append(delay)
            states.append(json.loads(self.health_file().read_text())['state'])
            if len(sleeps) == 8:
                self.store.request_cancel(PARENT)
        self.fake.fail_read = True
        with patch('codex_plus.background.time.sleep', sleep):
            self.observer.run()
        self.assertLessEqual(max(sleeps), 5)
        self.assertIn('error', states)
        self.assertEqual(json.loads(self.health_file().read_text())['state'], 'idle')

    def test_run_ready_callback_recovers_transient_rpc_health_while_parent_active(self):
        self.register()
        self.fake.complete()
        original = self.fake.call
        failed = []
        def call(method, params):
            if method == 'thread/read' and params['threadId'] == PARENT and not failed:
                failed.append(True)
                raise ConnectionError('private server detail')
            return original(method, params)
        self.fake.call = call
        states = []
        def sleep(delay):
            view = json.loads(self.health_file().read_text())
            states.append(view['state'])
            callback = self.callbacks()[0]
            self.assertEqual(callback['state'], 'ready')
            self.assertEqual(view['jobs'], 0)
            if len(states) == 1:
                self.assertIn('RPC unavailable (ConnectionError)', callback['error'])
                self.assertNotIn('private server detail', callback['error'])
            else:
                self.assertIsNone(callback['error'])
                self.store.request_cancel(PARENT)
        with patch('codex_plus.background.time.sleep', sleep):
            self.observer.run()
        self.assertEqual(states, ['error', 'waiting'])
        self.assertEqual(self.fake.enqueues, 0)
        self.assertEqual(json.loads(self.health_file().read_text())['state'], 'idle')

    def test_run_preserves_ready_native_and_supervisor_warnings(self):
        warnings = ('Keyed callback waits for native queue idempotency support.',
                    'Supervisor restart limit reached; observer recovery needs attention.')
        for index, warning in enumerate(warnings):
            with self.subTest(warning=warning):
                self.fake.child_turns = [{'id': f'warning-{index}', 'status': 'inProgress'}]
                self.register()
                self.fake.complete()
                self.observer.step()
                callback = next(row for row in self.callbacks() if row['state'] == 'ready')
                if index == 0:
                    self.store.claim_callback(callback, native=True)
                    self.observer.recover()
                    self.store.reconcile_evidence(next(row for row in self.callbacks() if row['id'] == callback['id']),
                                                  receipt_absent=True)
                    self.store.next_callback(PARENT, native=False)
                else:
                    self.store.supervisor_failed(PARENT)
                def sleep(delay):
                    self.assertEqual(json.loads(self.health_file().read_text())['state'], 'error')
                    current = next(row for row in self.callbacks() if row['id'] == callback['id'])
                    self.assertEqual(current['error'], warning)
                    self.store.request_cancel(PARENT)
                with patch('codex_plus.background.time.sleep', sleep):
                    self.observer.run()

    def test_long_batch_summarizes_three_jobs_and_more(self):
        processes = [str(n) for n in range(6)]
        self.fake.running = [{'itemId': f'command-{n}', 'processId': n} for n in processes]
        background.register(self.store, self.fake, PARENT, [], processes, 'batch')
        self.fake.running = []
        self.fake.entries = [{'turnId': 'origin', 'item': {'type': 'commandExecution',
                             'id': f'command-{n}', 'processId': n, 'status': 'completed', 'exitCode': 0}}
                             for n in processes]
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.payload().count('process '), 3)
        self.assertIn('+3 more', self.payload())
        self.assertLess(len(self.payload()), 1000)

    def test_worker_path_and_uuid_aliases_register_one_job(self):
        result = background.register(self.store, self.fake, PARENT, ['/root/work', CHILD], [], 'one worker')
        self.assertEqual(len(result['armed']), 1)
        self.assertEqual(len(self.store.status(PARENT)['jobs']), 1)
        self.fake.complete()
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len(self.fake.starts), 1)

    def test_active_then_idle_success_failed_interrupted(self):
        for status in ('completed', 'failed', 'interrupted'):
            with self.subTest(status=status):
                self.fake.child_turns = [{'id': status, 'status': 'inProgress'}]
                self.fake.parent_status = 'active'
                self.fake.parent_turns[0]['status'] = 'inProgress'
                self.register()
                self.fake.complete(status)
                self.observer.step()
                self.assertEqual(self.fake.enqueues, len(self.fake.starts))
                self.fake.idle()
                self.observer.step()
        self.assertEqual(self.fake.enqueues, 3)
        self.assertEqual(len(self.fake.starts), 3)
        self.assertTrue(all(c['state'] == 'delivered' for c in self.callbacks()))

    def test_notloaded_approval_active_never_wake(self):
        self.register()
        self.fake.complete()
        for state in ('notLoaded', 'active', 'systemError'):
            self.fake.parent_status = state
            self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.assertFalse(any(method == 'thread/resume' for method, _ in self.fake.calls))

    def test_generation_change_resumes_registered_parent_and_loses_old_worker(self):
        self.register()
        self.fake.idle()
        self.fake.parent_status = 'notLoaded'
        self.fake.generation = 'boot:200:2'
        self.assertFalse(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'lost')
        resumes = [params for method, params in self.fake.calls if method == 'thread/resume']
        self.assertEqual(resumes, [{'threadId': PARENT, 'excludeTurns': True}])
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(self.store.status(PARENT)['parks'][0]['state'], 'complete')
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 1)

    def test_generation_loss_reports_unknown_process_without_rerun(self):
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        background.register(self.store, self.fake, PARENT, [], ['9'], 'held shell')
        self.fake.idle()
        self.fake.generation = 'boot:200:2'
        self.fake.running = []
        self.observer.step()
        job = self.store.status(PARENT)['jobs'][0]
        self.assertEqual(job['status'], 'lost')
        self.assertIsNone(job['exit_code'])
        message = next(params['input'][0]['text'] for method, params in self.fake.calls
                       if method == 'thread/queue/add')
        self.assertIn('outcome=UNKNOWN', message)
        self.assertIn('Do not rerun', message)

    def test_generation_loss_keeps_canonical_anchor_completion(self):
        self.register()
        self.fake.complete('failed')
        self.fake.child_turns.insert(0, {'id': 'new-work', 'status': 'inProgress'})
        self.fake.idle()
        self.fake.generation = 'boot:200:2'
        self.observer.step()
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'failed')

    def test_replacement_loss_survives_observer_crash_before_job_poll(self):
        self.register()
        self.fake.idle()
        self.fake.generation = 'boot:200:2'
        with patch.object(self.observer, 'poll_jobs', side_effect=RuntimeError('observer crash')):
            with self.assertRaises(RuntimeError):
                self.observer.step()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'lost')
        self.assertEqual(self.fake.enqueues, 1)

    def test_cancelled_parent_not_resumed_after_generation_change(self):
        self.register()
        self.store.request_cancel(PARENT)
        self.fake.parent_status = 'notLoaded'
        self.fake.generation = 'boot:200:2'
        self.assertFalse(self.observer.step())
        self.assertFalse(any(method == 'thread/resume' for method, _ in self.fake.calls))

    def test_ready_callback_resumes_after_restart_without_armed_job(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'completed')
        self.fake.idle()
        self.fake.parent_status = 'notLoaded'
        self.fake.generation = 'boot:200:2'
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len(self.fake.starts), 1)

    def test_replacement_keeps_active_and_foreign_queue_gates(self):
        self.register()
        self.fake.generation = 'boot:200:2'
        self.observer.step()
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'lost')
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.idle()
        self.fake.queue.append({'id': 'foreign', 'clientUserMessageId': 'user-input'})
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.assertFalse(any(method == 'thread/resume' for method, _ in self.fake.calls))

    def test_cold_resume_waits_for_foreign_queue_then_recovers(self):
        self.register()
        self.fake.idle()
        self.fake.parent_status = 'notLoaded'
        self.fake.generation = 'boot:200:2'
        self.fake.queue.append({'id': 'foreign', 'clientUserMessageId': 'user-input'})
        self.assertTrue(self.observer.step())
        self.assertFalse(any(method == 'thread/resume' for method, _ in self.fake.calls))
        self.fake.queue.clear()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 1)

    def test_interrupted_origin_at_restart_is_conservatively_cancelled(self):
        self.register()
        self.fake.parent_turns[0].update(status='interrupted', completedAt=123)
        self.fake.parent_status = 'notLoaded'
        self.fake.generation = 'boot:200:2'
        self.observer.step()
        self.assertEqual(self.store.status(PARENT)['parks'][0]['state'], 'cancelled')
        self.assertEqual(self.fake.enqueues, 0)
        self.assertFalse(any(method == 'thread/resume' for method, _ in self.fake.calls))

    def test_known_queue_history_lag_rechecks_without_retrying_enqueue(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_start = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        history = self.fake.entries
        self.fake.entries = []
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertTrue(self.observer.step())
        self.fake.entries = history
        self.assertFalse(self.observer.step())
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')
        self.assertEqual(self.fake.enqueues, 1)

    def test_known_queue_without_history_stops_after_bounded_rechecks(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_start = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        self.fake.entries = []
        active = [self.observer.step() for _ in range(9)]
        self.assertTrue(active[0])
        self.assertFalse(active[-1])
        self.assertEqual(self.fake.enqueues, 1)

    def test_same_generation_missing_process_remains_unknown(self):
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        background.register(self.store, self.fake, PARENT, [], ['9'], 'same generation shell')
        self.fake.running = []
        self.fake.idle()
        self.assertTrue(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'armed')
        self.assertEqual(self.fake.enqueues, 0)

    def test_old_store_migration_preserves_jobs_without_assuming_generation_loss(self):
        self.register()
        with sqlite3.connect(self.store.directory / 'background.sqlite') as db:
            db.execute('ALTER TABLE parks DROP COLUMN generation')
            db.execute('ALTER TABLE parks DROP COLUMN recovery_generation')
            db.execute('ALTER TABLE callbacks DROP COLUMN reconcile_attempts')
        self.store.close()
        self.store = Store(self.temp.name)
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.fake.idle()
        self.fake.generation = 'boot:200:2'
        self.assertTrue(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'armed')
        self.assertEqual(self.store.status(PARENT)['parks'][0]['generation'], self.fake.generation)
        self.assertEqual(self.fake.enqueues, 0)

    def native(self):
        self.fake = NativeFake()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)

    def test_native_before_enqueue_crash_retries_once_with_durable_key(self):
        self.native()
        self.register()
        self.fake.complete()
        self.fake.idle()
        def crash(point):
            if point == 'before_enqueue':
                raise SystemExit(79)
        self.observer.fault = crash
        with self.assertRaises(SystemExit):
            self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')

    def test_native_ack_loss_consumed_receipt_does_not_need_history(self):
        self.native()
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_add = self.fake.consume_add = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        self.fake.entries = []
        self.fake.fail_add = False
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')

    def test_native_cancel_before_enqueue_creates_tombstone(self):
        self.native()
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.observer.fault = lambda point: (_ for _ in ()).throw(SystemExit(79)) if point == 'before_enqueue' else None
        with self.assertRaises(SystemExit):
            self.observer.step()
        self.store.request_cancel(PARENT)
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        callback = self.callbacks()[0]
        self.assertEqual(callback['state'], 'cancelled')
        self.assertEqual(self.fake.receipts[callback['client']], {'queuedSubmission': None, 'receiptState': 'cancelled'})
        self.assertEqual(self.fake.enqueues, 0)

    def test_native_upgrade_does_not_retry_legacy_ambiguous_callback(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.observer.fault = lambda point: (_ for _ in ()).throw(SystemExit(79)) if point == 'before_enqueue' else None
        with self.assertRaises(SystemExit):
            self.observer.step()
        self.native()
        self.fake.idle()
        self.observer.recover()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.callbacks()[0]['state'], 'uncertain')
        self.assertEqual(self.fake.enqueues, 0)
        self.assertFalse(any(method.startswith('thread/queue/receipt') for method, _ in self.fake.calls))

    def test_native_callback_payload_stays_fixed_when_other_jobs_finish(self):
        self.native()
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        background.register(self.store, self.fake, PARENT, ['/root/work'], ['9'], 'two jobs')
        self.fake.complete()
        self.fake.idle()
        self.observer.fault = lambda point: (_ for _ in ()).throw(SystemExit(79)) if point == 'before_enqueue' else None
        with self.assertRaises(SystemExit):
            self.observer.step()
        payload = self.callbacks()[0]['message']
        self.assertIn('Remaining registered jobs: 1', payload)
        self.fake.running = []
        self.fake.entries = [{'turnId': 'origin', 'item': {'type': 'commandExecution', 'id': 'held',
                             'processId': '9', 'status': 'completed', 'exitCode': 0}}]
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        submitted = next(params['input'][0]['text'] for method, params in self.fake.calls
                         if method == 'thread/queue/add')
        self.assertEqual(submitted, payload)

    def test_native_pending_cancellation_removes_owned_queue(self):
        self.native()
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_add = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        self.store.request_cancel(PARENT)
        self.fake.fail_add = False
        self.assertFalse(self.observer.step())
        self.assertEqual(self.callbacks()[0]['state'], 'cancelled')
        self.assertEqual(self.fake.queue, [])
        self.assertEqual(self.fake.starts, [])

    def test_native_receipt_identity_mismatch_never_delivers(self):
        self.native()
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_add = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        callback = self.callbacks()[0]
        self.fake.receipts[callback['client']]['queuedSubmission']['clientUserMessageId'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.observer.step()
        self.assertEqual(self.callbacks()[0]['state'], 'uncertain')
        self.assertEqual(self.fake.starts, [])

    def test_native_start_ack_does_not_complete_before_input_is_durable(self):
        self.native()
        self.register()
        self.fake.complete()
        self.fake.idle()
        original = self.fake.call
        def accept_without_persistence(method, params):
            if method == 'thread/queue/start':
                self.fake.calls.append((method, dict(params)))
                self.fake.starts.append(params['queuedSubmissionId'])
                self.fake.parent_status = 'active'
                return {'turn': {'id': 'not-durable'}}
            return original(method, params)
        self.fake.call = accept_without_persistence
        self.assertTrue(self.observer.step())
        self.assertIn(self.callbacks()[0]['state'], ('pending', 'starting'))
        self.assertEqual(self.store.status(PARENT)['parks'][0]['state'], 'armed')
        self.fake.call = original
        self.fake.parent_status = 'notLoaded'
        self.fake.generation = 'boot:200:2'
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len([entry for entry in self.fake.entries if entry['item']['type'] == 'userMessage']), 1)

    def test_keyed_ready_callback_waits_through_server_downgrade(self):
        self.native()
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.observer.fault = lambda point: (_ for _ in ()).throw(SystemExit(79)) if point == 'before_enqueue' else None
        with self.assertRaises(SystemExit):
            self.observer.step()
        self.observer.recover()
        self.observer.reconcile(self.callbacks()[0], [])
        self.assertEqual(self.callbacks()[0]['state'], 'ready')
        self.fake.initialize_result = {}
        self.fake.generation = 'boot:200:2'
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.assertTrue(self.observer.step())
        self.assertEqual(self.callbacks()[0]['idempotent'], 1)
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.initialize_result = {'queueIdempotency': True}
        self.fake.generation = 'boot:300:3'
        self.assertFalse(self.observer.step())
        self.assertEqual(self.callbacks()[0]['idempotent'], 1)
        self.assertEqual(self.fake.enqueues, 1)

    def test_keyed_pending_downgrade_never_resumes_starts_or_deletes(self):
        self.native()
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_add = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        self.fake.fail_add = False
        self.fake.initialize_result = {}
        self.fake.generation = 'boot:200:2'
        self.fake.parent_status = 'notLoaded'
        self.assertTrue(self.observer.step())
        self.assertFalse(any(method in ('thread/resume', 'thread/queue/start') for method, _ in self.fake.calls))
        self.store.request_cancel(PARENT)
        self.assertTrue(self.observer.step())
        self.assertFalse(any(method == 'thread/queue/delete' for method, _ in self.fake.calls))
        self.fake.initialize_result = {'queueIdempotency': True}
        self.assertFalse(self.observer.step())
        self.assertEqual(self.callbacks()[0]['state'], 'cancelled')
        self.assertEqual(self.fake.queue, [])

    def test_supervisor_restarts_crash_but_stops_on_normal_exit(self):
        with patch('codex_plus.background.subprocess.Popen') as popen, patch('codex_plus.background.time.sleep'):
            popen.return_value.wait.side_effect = [-9, 79, 0]
            background.supervise(self.store, PARENT, self.temp.name)
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(popen.call_args[0][0][3:5], ['observe', '--parent'])

    def test_supervisor_crash_retries_are_bounded_and_durable(self):
        self.register()
        with patch('codex_plus.background.subprocess.Popen') as popen, patch('codex_plus.background.time.sleep'):
            popen.return_value.wait.return_value = 79
            background.supervise(self.store, PARENT, self.temp.name)
        self.assertEqual(popen.call_count, 6)
        self.assertIn('Supervisor restart limit', self.store.status(PARENT)['parks'][0]['error'])

    def test_actual_observer_kill_restarts_and_cancellation_stops_supervisor(self):
        self.register()
        supervisor = subprocess.Popen([sys.executable, '-m', 'codex_plus.background', 'supervise', '--parent', PARENT],
            env=dict(os.environ, CODEX_HOME=self.temp.name), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        def child_after(previous=None):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                children = Path(f'/proc/{supervisor.pid}/task/{supervisor.pid}/children').read_text().split()
                if children and children[0] != previous:
                    return children[0]
                time.sleep(0.03)
            self.fail('supervisor did not produce the expected observer child')
        try:
            first = child_after()
            os.kill(int(first), signal.SIGKILL)
            second = child_after(first)
            self.assertNotEqual(first, second)
            self.store.request_cancel(PARENT)
            self.assertEqual(supervisor.wait(timeout=5), 0)
        finally:
            if supervisor.poll() is None:
                os.killpg(supervisor.pid, signal.SIGTERM)
                supervisor.wait(timeout=5)

    def test_direct_child_unique_and_parent_validation(self):
        self.fake.children[0]['parentThreadId'] = 'other'
        with self.assertRaises(ValueError):
            self.register()
        self.fake.children[0]['parentThreadId'] = PARENT
        self.fake.children.append(dict(self.fake.children[0], id='other'))
        with self.assertRaises(ValueError):
            self.register()
        self.assertEqual(self.store.status(PARENT)['jobs'], [])

    def test_reused_child_anchor_and_historical_not_replayed(self):
        self.fake.child_turns = [{'id': 'new', 'status': 'inProgress'}, {'id': 'old', 'status': 'completed'}]
        self.register()
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.child_turns.insert(0, {'id': 'newer', 'status': 'inProgress'})
        self.fake.child_turns[1]['status'] = 'completed'
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)

    def test_completion_before_park_returns_finished_no_future_wake(self):
        self.fake.complete()
        self.assertEqual(self.register()['state'], 'already_finished')
        self.assertEqual(self.register()['state'], 'already_finished')
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.assertEqual(self.store.status(PARENT)['jobs'], [])
        self.assertEqual(self.store.status(PARENT)['parks'], [])

    def test_process_absence_is_not_completion_and_updates_same_item(self):
        command = {'type': 'commandExecution', 'id': 'command', 'processId': '42', 'status': 'inProgress', 'exitCode': None}
        self.fake.running = [{'itemId': 'command', 'processId': '42'}]
        self.fake.entries = [{'turnId': 'origin', 'item': command}]
        background.register(self.store, self.fake, PARENT, [], ['42'], 'command result')
        self.fake.idle()
        self.fake.running = []
        self.fake.entries = []
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        command.update(status='completed', exitCode=2)
        self.fake.entries = [{'turnId': 'origin', 'item': command}]
        self.observer.step()
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'failed')
        self.assertEqual(self.fake.enqueues, 1)

    def test_cancel_before_and_after_enqueue(self):
        self.register()
        self.store.request_cancel(PARENT)
        self.fake.complete()
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.parent_status = 'active'
        self.fake.parent_turns[0]['status'] = 'inProgress'
        self.fake.child_turns[0].update(id='second', status='inProgress')
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_add = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        self.store.request_cancel(PARENT)
        self.fake.fail_add = False
        self.observer.step()
        self.assertEqual(self.fake.starts, [])
        self.assertEqual(self.fake.queue, [])
        self.assertEqual(self.callbacks()[0]['state'], 'cancelled')

    def test_parent_origin_interrupted_cancels(self):
        self.register()
        self.fake.parent_turns[0].update(status='interrupted', completedAt=123)
        self.fake.parent_status = 'idle'
        self.fake.complete()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.assertEqual(self.store.status(PARENT)['parks'][0]['state'], 'cancelled')

    def test_transient_origin_interruption_preserves_exact_job_and_delivers_once(self):
        self.register()
        self.observer.step()
        original = self.store.status(PARENT)['jobs'][0]['id']
        self.fake.parent_status = 'idle'
        self.fake.parent_turns[0].update(status='interrupted', completedAt=None)
        self.assertTrue(self.observer.step())
        state = self.store.status(PARENT)
        self.assertEqual(state['parks'][0]['state'], 'armed')
        self.assertEqual(state['jobs'][0]['status'], 'armed')
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.parent_turns[0].update(status='completed', completedAt=123)
        self.fake.complete()
        self.assertFalse(self.observer.step())
        self.assertFalse(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['id'], original)
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))
        self.assertIn('LIVE COMPLETION', self.payload())

    def test_transient_worker_interruption_does_not_finish_exact_job(self):
        self.register()
        self.fake.idle()
        self.fake.child_turns[0].update(status='interrupted', completedAt=None)
        self.assertTrue(self.observer.step())
        state = self.store.status(PARENT)
        self.assertEqual(state['jobs'][0]['status'], 'armed')
        self.assertIsNone(state['jobs'][0]['callback'])
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.complete()
        self.assertFalse(self.observer.step())
        self.assertFalse(self.observer.step())
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))

    def test_pending_origin_survives_restart_then_reports_once(self):
        self.register()
        self.observer.step()
        self.fake.idle()
        self.fake.complete()
        self.fake.parent_turns[0].update(status='interrupted', completedAt=None)
        self.observer.step()
        callback = self.callbacks()[0]
        self.store.monitoring_failed(PARENT, 'ConnectionError')
        self.store.monitoring_succeeded(PARENT)
        state = self.store.status(PARENT)
        self.assertEqual(state['health']['state'], 'error')
        self.assertIn('positive terminal evidence', state['parks'][0]['error'])
        self.assertIn('ConnectionError', state['parks'][0]['monitoring_error'])
        self.assertEqual(state['parks'][0]['origin_pending'], 1)
        self.store.close()
        self.store = Store(self.temp.name)
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertTrue(self.observer.step())
        self.assertEqual(self.fake.enqueues, 0)
        self.assertFalse(self.store.can_resume(PARENT, [], False))
        self.fake.parent_turns[0].update(status='completed', completedAt=123)
        self.assertFalse(self.observer.step())
        delivered = self.callbacks()[0]
        self.assertEqual((delivered['id'], delivered['client']), (callback['id'], callback['client']))
        self.assertIn('REPORT ONLY', self.payload())
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))

    def test_pending_worker_survives_generation_change_without_lost_outcome(self):
        self.register()
        self.observer.step()
        self.fake.idle()
        self.fake.generation = 'boot:200:2'
        self.fake.child_runtime_status = 'systemError'
        self.fake.child_turns[0].update(status='interrupted', completedAt=None)
        self.assertTrue(self.observer.step())
        state = self.store.status(PARENT)
        self.assertEqual((state['jobs'][0]['status'], state['jobs'][0]['interruption_pending']), ('armed', 1))
        self.assertEqual(state['parks'][0]['recovery_generation'], self.fake.generation)
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.fake.child_turns = []
        self.assertTrue(self.observer.step())
        self.fake.child_turns = [{'id': 'work', 'status': 'completed'}]
        self.assertFalse(self.observer.step())
        self.assertIn('REPORT ONLY', self.payload())
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))

    def test_generation_loss_with_unloaded_ambiguous_worker_reports_unknown(self):
        self.register()
        self.observer.step()
        self.fake.idle()
        self.fake.generation = 'boot:200:2'
        self.fake.child_turns[0].update(status='interrupted', completedAt=None)
        self.fake.child_runtime_status = 'notLoaded'
        self.assertFalse(self.observer.step())
        state = self.store.status(PARENT)
        self.assertEqual(state['jobs'][0]['status'], 'lost')
        self.assertEqual(state['jobs'][0]['interruption_pending'], 0)
        self.assertIn('REPORT ONLY', self.payload())
        self.assertIn('outcome=UNKNOWN', self.payload())
        self.assertFalse(any(method == 'thread/resume' and params['threadId'] == CHILD
                             for method, params in self.fake.calls))
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))

    def test_generation_ambiguous_projection_with_exact_active_worker_stays_live(self):
        self.register()
        self.observer.step()
        self.fake.generation = 'boot:200:2'
        self.store.observe_generation(PARENT, self.fake.generation)
        self.fake.child_runtime_status = 'active'
        original = self.fake.call
        projected = []
        def call(method, params):
            if method == 'thread/turns/list' and params['threadId'] == CHILD and not projected:
                projected.append(True)
                return {'data': [{'id': 'work', 'status': 'interrupted', 'completedAt': None}], 'nextCursor': None}
            return original(method, params)
        self.fake.call = call
        self.observer.poll_jobs()
        job = self.store.status(PARENT)['jobs'][0]
        self.assertEqual((job['status'], job['interruption_pending'], job['recovery_pending']), ('armed', 0, 0))
        self.fake.complete()
        self.fake.idle()
        self.assertFalse(self.observer.step())
        self.assertIn('LIVE COMPLETION', self.payload())

    def test_generation_unresolved_worker_keeps_proof_after_rpc_failure_and_restart(self):
        self.register()
        self.observer.step()
        self.fake.idle()
        self.fake.generation = 'boot:200:2'
        self.fake.child_turns[0].update(status='interrupted', completedAt=None)
        original = self.fake.call
        def call(method, params):
            if method == 'thread/read' and params['threadId'] == CHILD:
                raise ConnectionError('unavailable child runtime')
            return original(method, params)
        self.fake.call = call
        with self.assertRaises(ConnectionError):
            self.observer.step()
        park = self.store.status(PARENT)['parks'][0]
        self.assertEqual(park['recovery_generation'], self.fake.generation)
        self.store.close()
        self.store = Store(self.temp.name)
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.fake.call = original
        self.assertFalse(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'lost')
        self.assertIn('REPORT ONLY', self.payload())
        self.assertIn('outcome=UNKNOWN', self.payload())

    def test_ambiguous_latest_worker_registration_is_armed(self):
        self.fake.child_turns[0].update(status='interrupted', completedAt=None)
        result = self.register()
        self.assertEqual(result['state'], 'armed')
        self.assertEqual(result['already_finished'], [])
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['interruption_pending'], 1)
        self.fake.idle()
        self.assertTrue(self.observer.step())
        self.fake.complete()
        self.assertFalse(self.observer.step())
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))

    def test_exact_rollout_completion_resolves_origin_and_worker_projection(self):
        self.register()
        self.fake.idle()
        self.fake.parent_turns[0].update(status='interrupted', completedAt=None)
        self.fake.child_turns[0].update(status='interrupted', completedAt=None)
        self.terminal_rollout(PARENT, 'origin', 'task_complete')
        self.terminal_rollout(CHILD, 'work', 'task_complete')
        with patch('codex_plus.background.state_home', side_effect=AssertionError('use Store home')):
            self.assertFalse(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'completed')
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))

    def test_exact_rollout_abort_cancels_and_preserves_provenance(self):
        self.register()
        self.observer.step()
        self.store.monitoring_failed(PARENT, 'ConnectionError')
        self.fake.idle()
        self.fake.complete()
        self.fake.parent_turns[0].update(status='interrupted', completedAt=None)
        self.terminal_rollout(PARENT, 'origin', 'turn_aborted', reason='interrupted')
        self.assertFalse(self.observer.step())
        park = self.store.status(PARENT)['parks'][0]
        self.assertEqual(park['state'], 'cancelled')
        self.assertEqual(park['cancel_kind'], 'confirmed_origin')
        self.assertGreater(park['cancelled_at'], 0)
        evidence = json.loads(park['cancel_evidence'])
        self.assertEqual((evidence['source'], evidence['event'], evidence['status']), ('rollout', 'turn_aborted', 'interrupted'))
        self.assertEqual(evidence['timestamp'], '2026-10-15T01:02:03Z')
        self.assertEqual(evidence['observer'], self.observer.run_id)
        self.assertIn('ConnectionError', evidence['previous_error'])
        self.assertEqual(self.fake.enqueues, 0)
        self.store.request_cancel(PARENT)
        after = self.store.status(PARENT)['parks'][0]
        self.assertEqual((after['cancelled_at'], after['cancel_kind'], after['cancel_evidence']),
                         (park['cancelled_at'], park['cancel_kind'], park['cancel_evidence']))

    def test_exact_rollout_abort_finishes_worker_without_cancelling_origin(self):
        self.register()
        self.fake.idle()
        self.fake.child_turns[0].update(status='interrupted', completedAt=None)
        self.terminal_rollout(CHILD, 'work', 'turn_aborted', reason='interrupted')
        self.assertFalse(self.observer.step())
        state = self.store.status(PARENT)
        self.assertEqual(state['jobs'][0]['status'], 'interrupted')
        self.assertEqual(state['parks'][0]['state'], 'complete')
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))

    def test_api_terminal_status_and_positive_metadata_need_no_rollout_read(self):
        def classify(status, completed):
            return background.classify_turn(self.fake, self.temp.name, CHILD,
                                            {'id': 'work', 'status': status, 'completedAt': completed})[0]
        for status, completed in (('completed', None), ('failed', None), ('interrupted', 123), ('interrupted', 0.5)):
            self.assertEqual(classify(status, completed), status)
        self.assertFalse(self.fake.calls)

    def test_invalid_end_metadata_and_untrusted_rollout_remain_pending(self):
        self.register()
        self.fake.idle()
        self.fake.complete()
        path = self.terminal_rollout(PARENT, 'other-turn', 'turn_aborted', reason='interrupted')
        for metadata in (None, 0, -1, True, '123', float('nan'), float('inf'), 10 ** 1000):
            with self.subTest(completedAt=repr(metadata)[:20]):
                self.fake.parent_turns[0].update(status='interrupted', completedAt=metadata)
                self.assertTrue(self.observer.step())
                self.assertEqual(self.store.status(PARENT)['parks'][0]['state'], 'armed')
                self.assertEqual(self.fake.enqueues, 0)
        for contents in ('malformed\n', json.dumps({'type': 'session_meta', 'payload': {'id': CHILD}}) + '\n'):
            path.write_text(contents)
            self.assertTrue(self.observer.step())
            self.assertEqual(self.fake.enqueues, 0)
        path.unlink()
        self.assertTrue(self.observer.step())
        self.store.request_cancel(PARENT)
        park = self.store.status(PARENT)['parks'][0]
        self.assertEqual((park['state'], park['cancel_kind']), ('cancelled', 'explicit'))
        self.assertEqual(self.store.status(PARENT)['health']['state'], 'idle')
        self.fake.parent_turns[0]['status'] = 'completed'
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 0)

    def test_ambiguous_origin_read_failure_or_wrong_identity_is_pending(self):
        self.register()
        self.fake.idle()
        self.fake.parent_turns[0]['status'] = 'interrupted'
        self.fake.complete()
        self.terminal_rollout(PARENT, 'origin', 'turn_aborted', reason='interrupted')
        original = self.fake.call
        for mode in ('failure', 'identity'):
            def call(method, params):
                result = original(method, params)
                if method == 'thread/read' and params['threadId'] == PARENT:
                    if mode == 'failure':
                        raise ConnectionError('private error must not be recorded')
                    result['thread']['id'] = CHILD
                return result
            self.fake.call = call
            self.observer.cancel_interrupted()
            park = self.store.status(PARENT)['parks'][0]
            self.assertEqual((park['state'], park['origin_pending']), ('armed', 1))
            self.assertNotIn('private error', repr(park))
        self.fake.call = original
        self.assertFalse(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['parks'][0]['state'], 'cancelled')

    def test_pending_worker_does_not_block_completed_command(self):
        self.fake.running = [{'itemId': 'command', 'processId': '42'}]
        background.register(self.store, self.fake, PARENT, [CHILD], ['42'], 'mixed work')
        self.fake.idle()
        self.fake.child_turns[0]['status'] = 'interrupted'
        self.fake.running = []
        self.fake.entries = [{'turnId': 'origin', 'item': {'id': 'command', 'type': 'commandExecution',
                             'processId': '42', 'status': 'completed', 'exitCode': 0}}]
        self.assertTrue(self.observer.step())
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))
        state = self.store.status(PARENT)
        self.assertEqual({j['kind']: j['status'] for j in state['jobs']}, {'worker': 'armed', 'process': 'completed'})
        self.assertEqual(state['health']['state'], 'error')
        self.fake.complete()
        self.assertFalse(self.observer.step())
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (2, 2))

    def test_pending_origin_is_rechecked_transactionally_before_both_claims(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        ready = self.callbacks()[0]
        self.store.origin_pending(PARENT, 'origin', True)
        self.assertIsNone(self.store.claim_callback(ready))
        self.assertIsNone(self.callbacks()[0]['message'])
        self.store.origin_pending(PARENT, 'origin', False)
        claimed = self.store.claim_callback(ready)
        self.store.dispatch_result(claimed, queued='owned')
        pending = self.callbacks()[0]
        self.store.origin_pending(PARENT, 'origin', True)
        self.assertIsNone(self.store.claim_callback(pending, starting=True))
        self.assertFalse(self.store.can_resume(PARENT, [], False))
        self.assertEqual(self.callbacks()[0]['state'], 'pending')

    def test_projection_between_enqueue_and_start_blocks_start_until_resolved(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        def fault(point):
            if point == 'after_enqueue':
                self.fake.parent_turns[0].update(status='interrupted', completedAt=None)
        self.observer.fault = fault
        self.assertTrue(self.observer.step())
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 0))
        self.assertEqual(self.callbacks()[0]['state'], 'pending')
        self.fake.parent_turns[0]['status'] = 'completed'
        self.assertFalse(self.observer.step())
        self.assertEqual((self.fake.enqueues, len(self.fake.starts)), (1, 1))

    def test_pending_origin_blocks_generation_recovery_resume(self):
        self.register()
        self.fake.parent_status = 'notLoaded'
        self.fake.parent_turns[0]['status'] = 'interrupted'
        self.fake.generation = 'boot:200:2'
        self.fake.complete()
        self.assertTrue(self.observer.step())
        self.assertFalse(any(method == 'thread/resume' for method, _ in self.fake.calls))
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.parent_turns[0]['status'] = 'completed'
        self.assertFalse(self.observer.step())
        self.assertEqual(sum(method == 'thread/resume' for method, _ in self.fake.calls), 1)
        self.assertIn('REPORT ONLY', self.payload())

    def test_recovery_rechecks_cancel_and_projection_after_queue_read(self):
        self.register()
        self.fake.idle()
        self.fake.parent_status = 'notLoaded'
        self.fake.generation = 'boot:200:2'
        original = self.fake.call
        def call(method, params):
            result = original(method, params)
            if method == 'thread/queue/list':
                self.fake.parent_turns[0].update(status='interrupted', completedAt=None)
            return result
        self.fake.call = call
        self.assertTrue(self.observer.step())
        self.assertFalse(any(method == 'thread/resume' for method, _ in self.fake.calls))
        self.fake.parent_turns[0]['status'] = 'completed'
        def cancelling_call(method, params):
            result = original(method, params)
            if method == 'thread/queue/list':
                self.store.request_cancel(PARENT)
            return result
        self.fake.call = cancelling_call
        self.assertFalse(self.observer.step())
        self.assertFalse(any(method == 'thread/resume' for method, _ in self.fake.calls))
        self.assertEqual(self.fake.enqueues, 0)

    def test_concurrent_legacy_migration_keeps_unknown_cancellation_provenance(self):
        with tempfile.TemporaryDirectory() as home:
            directory = Path(home) / 'codex-plus'
            directory.mkdir()
            with sqlite3.connect(directory / 'background.sqlite') as db:
                db.executescript('''
                    CREATE TABLE parks (id TEXT PRIMARY KEY,parent TEXT,origin TEXT,reason TEXT,state TEXT,deadline REAL,error TEXT);
                    CREATE TABLE jobs (id TEXT PRIMARY KEY,park TEXT,kind TEXT,thread TEXT,turn TEXT,item TEXT,process TEXT,status TEXT,exit_code INTEGER,callback TEXT);
                    CREATE TABLE callbacks (id TEXT PRIMARY KEY,parent TEXT,client TEXT,state TEXT,queue TEXT,error TEXT);
                ''')
                db.execute("INSERT INTO parks VALUES ('legacy',?,'origin','old cancellation','cancelled',123,NULL)", (PARENT,))
            gate = multiprocessing.Event()
            workers = [multiprocessing.Process(target=migrate_store, args=(home, gate)) for _ in range(4)]
            try:
                for worker in workers:
                    worker.start()
                gate.set()
                for worker in workers:
                    worker.join(10)
                    self.assertEqual(worker.exitcode, 0)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(5)
            with_store = Store(home)
            try:
                with_store.request_cancel(PARENT)
                park = with_store.status(PARENT)['parks'][0]
                self.assertEqual(park['state'], 'cancelled')
                self.assertIsNone(park['cancelled_at'])
                self.assertIsNone(park['cancel_kind'])
                self.assertIsNone(park['cancel_evidence'])
            finally:
                with_store.close()

    def test_lost_add_ack_positive_pending_reconcile(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_add = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        self.assertEqual(self.callbacks()[0]['state'], 'uncertain')
        self.fake.fail_add = False
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len(self.fake.starts), 1)
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')

    def test_lost_add_ack_consumed_reconcile(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_add = self.fake.consume_add = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')

    def test_crash_before_attempt_stays_uncertain_no_retry(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        def fault(point):
            if point == 'before_enqueue':
                raise SystemExit(91)
        self.observer.fault = fault
        with self.assertRaises(SystemExit):
            self.observer.step()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 0)
        self.assertEqual(self.callbacks()[0]['state'], 'uncertain')

    def test_start_ack_loss_consumed_and_restart_no_duplicate(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_start = True
        with self.assertRaises(TimeoutError):
            self.observer.step()
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len(self.fake.starts), 1)
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')

    def test_other_user_queue_blocks_start_and_enqueue(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.queue.append({'id': 'user', 'clientUserMessageId': 'user'})
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.queue.clear()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)

    def test_disconnect_and_restart(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.fake.fail_read = True
        with self.assertRaises(ConnectionError):
            self.observer.step()
        self.fake.fail_read = False
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)

    def test_state_permissions_no_transcripts(self):
        self.register()
        self.assertEqual(os.stat(self.store.directory).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.store.directory / 'background.sqlite').st_mode & 0o777, 0o600)
        self.assertNotIn('aggregatedOutput', repr(self.store.status(PARENT)))

    def test_registry_only_origin_resolves_after_restart(self):
        self.fake.running = [{'itemId': 'transient', 'processId': '73'}]
        background.register(self.store, self.fake, PARENT, [], ['73'], 'held command')
        original = self.store.status(PARENT)['jobs'][0]
        self.assertIsNone(original['turn'])
        self.fake.idle()
        self.fake.running = []
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.entries = [{'turnId': 'exact-origin', 'item': {'type': 'commandExecution',
                             'id': 'transient', 'processId': '73', 'status': 'completed', 'exitCode': 0}}]
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        after = self.store.status(PARENT)['jobs'][0]
        self.assertEqual(after['id'], original['id'])
        self.assertEqual(after['turn'], 'exact-origin')
        self.assertEqual(self.fake.enqueues, 1)
        self.fake.parent_status = 'active'
        self.fake.parent_turns[0]['status'] = 'inProgress'
        background.register(self.store, self.fake, PARENT, [], ['73'], 'same completed command')
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)

    def test_coalesce_worker_and_command_terminal_same_poll(self):
        self.fake.running = [{'itemId': 'command', 'processId': '42'}]
        background.register(self.store, self.fake, PARENT, [CHILD], ['42'], 'finite batch')
        self.fake.complete('failed')
        self.fake.running = []
        self.fake.entries = [{'turnId': 'origin', 'item': {'type': 'commandExecution', 'id': 'command',
                             'processId': '42', 'status': 'completed', 'exitCode': 0}}]
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len(self.store.status(PARENT)['jobs']), 2)
        self.assertEqual(len({j['callback'] for j in self.store.status(PARENT)['jobs']}), 1)

    def test_enqueue_then_crash_before_ack_positive_recovery(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        def fault(point):
            if point == 'after_enqueue':
                raise SystemExit(91)
        self.observer.fault = fault
        with self.assertRaises(SystemExit):
            self.observer.step()
        self.assertEqual(self.callbacks()[0]['state'], 'in_flight')
        self.observer = Observer(self.store, PARENT, lambda: self.fake)
        self.observer.recover()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len(self.fake.starts), 1)

    def test_notidle_start_retry_only_known_pending_queue(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        original = self.fake.call
        attempts = []
        def call(method, params):
            if method == 'thread/queue/start' and not attempts:
                attempts.append(params['queuedSubmissionId'])
                raise ValueError('NotIdle race')
            return original(method, params)
        self.fake.call = call
        with self.assertRaises(ValueError):
            self.observer.step()
        queue_id = self.fake.queue[0]['id']
        self.observer.step()
        self.assertEqual(attempts, [queue_id])
        self.assertEqual(self.fake.starts, [queue_id])
        self.assertEqual(self.fake.enqueues, 1)

    def test_foreign_queue_arrives_after_enqueue_blocks_start(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        original = self.fake.call
        def call(method, params):
            result = original(method, params)
            if method == 'thread/queue/add':
                self.fake.queue.append({'id': 'later-user', 'clientUserMessageId': 'user'})
            return result
        self.fake.call = call
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(self.fake.starts, [])
        self.assertEqual(self.callbacks()[0]['state'], 'pending')

    def test_cancel_uncertain_with_no_positive_evidence(self):
        self.register()
        self.fake.complete()
        self.fake.idle()
        self.observer.poll_jobs()
        self.observer.batch()
        self.store.claim_callback(self.callbacks()[0])
        self.store.request_cancel(PARENT)
        self.assertFalse(self.observer.step())
        self.assertEqual(self.callbacks()[0]['state'], 'cancel_uncertain')
        self.assertEqual(self.fake.enqueues, 0)

    def test_expired_work_never_fakes_completion(self):
        with patch('codex_plus.ledger.time.time', return_value=0):
            self.register()
        self.fake.idle()
        self.fake.complete()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 0)
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'expired')

    def test_interrupt_only_cancels_registration_from_that_origin(self):
        self.register()
        self.fake.parent_turns.insert(0, {'id': 'new-origin', 'status': 'inProgress'})
        self.fake.parent_turns[1].update(status='interrupted', completedAt=123)
        self.fake.child_turns = [{'id': 'new-work', 'status': 'inProgress'}]
        self.register()
        self.observer.step()
        states = {p['origin']: p['state'] for p in self.store.status(PARENT)['parks']}
        self.assertEqual(states, {'origin': 'cancelled', 'new-origin': 'armed'})

    def test_positive_running_registry_avoids_item_history(self):
        self.fake.running = [{'itemId': 'held', 'processId': '19'}]
        result = background.register(self.store, self.fake, PARENT, [], ['19'], 'held process')
        self.assertEqual(result['state'], 'armed')
        self.assertIsNone(self.store.status(PARENT)['jobs'][0]['turn'])
        self.observer.step()
        self.fake.idle()
        self.observer.step()
        self.assertFalse(any(method == 'thread/items/list' for method, _ in self.fake.calls))
        self.assertEqual(self.fake.enqueues, 0)
        self.fake.running = []
        self.observer.step()
        self.assertTrue(any(method == 'thread/items/list' for method, _ in self.fake.calls))
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'armed')
        self.assertEqual(self.fake.enqueues, 0)

    def test_duplicate_registration_truth_and_no_empty_park(self):
        self.assertEqual(self.register()['state'], 'armed')
        result = self.register()
        self.assertEqual(result['state'], 'already_registered')
        self.assertIsNone(result['park'])
        self.assertEqual(len(self.store.status(PARENT)['parks']), 1)
        self.fake.complete()
        self.fake.idle()
        self.observer.step()
        self.fake.parent_status = 'active'
        self.fake.parent_turns[0]['status'] = 'inProgress'
        result = self.register()
        self.assertEqual(result['state'], 'already_finished')
        self.assertEqual(result['already_finished'][0]['delivery'], 'delivered')
        self.assertEqual(len(self.store.status(PARENT)['parks']), 1)

    def test_cancelled_and_expired_identity_cannot_rearm(self):
        self.register()
        self.store.request_cancel(PARENT)
        with self.assertRaisesRegex(ValueError, 'cannot be rearmed'):
            self.register()
        self.fake.child_turns[0]['id'] = 'expired-work'
        result = self.register()
        self.store.expire_registration(result['park'])
        with self.assertRaisesRegex(ValueError, 'cannot be rearmed'):
            self.register()
        self.assertEqual(len(self.store.status(PARENT)['parks']), 2)

    def test_mixed_running_and_finished_registration_reports_both(self):
        self.fake.complete()
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        result = background.register(self.store, self.fake, PARENT, [CHILD], ['9'], 'mixed selections')
        self.assertEqual(result['state'], 'armed')
        self.assertEqual(len(result['armed']), 1)
        self.assertEqual(len(result['already_finished']), 1)
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['kind'], 'process')

    def test_finished_process_needs_no_future_wake(self):
        self.fake.entries = [{'turnId': 'past', 'item': {'type': 'commandExecution',
                             'id': 'done', 'processId': '9', 'status': 'completed', 'exitCode': 0}}]
        result = background.register(self.store, self.fake, PARENT, [], ['9'], 'already finished')
        self.assertEqual(result['state'], 'already_finished')
        self.assertEqual(self.store.status(PARENT)['parks'], [])

    def test_finished_selection_preserves_existing_unconsumed_callback(self):
        self.register()
        self.fake.complete()
        self.observer.step()
        result = self.register()
        self.assertEqual(result['state'], 'already_registered')
        self.assertEqual(result['already_registered'][0]['delivery'], 'ready')
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)

    def test_nonspawn_source_variants_id_matching_safe(self):
        for source in ({'subAgent': 'review'}, {'subAgent': 'compact'}, 'cli'):
            with self.subTest(source=source):
                self.fake.children[0]['source'] = source
                self.assertIn(background.register(self.store, self.fake, PARENT, [CHILD], [], 'ID selector')['state'],
                              ('armed', 'already_registered'))
                with self.assertRaisesRegex(ValueError, 'direct child'):
                    self.register()

    def test_pagination_bound_and_durable_sanitized_error_recovery(self):
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        background.register(self.store, self.fake, PARENT, [], ['9'], 'history bound')
        self.fake.running = []
        original = self.fake.call
        bounded_calls = []
        failing = [True]
        def call(method, params):
            if failing[0] and method == 'thread/items/list':
                bounded_calls.append(params.get('cursor'))
                return {'data': [], 'nextCursor': str(len(bounded_calls))}
            return original(method, params)
        self.fake.call = call
        observed = []
        def recover(delay):
            error = self.store.status(PARENT)['parks'][0]['error']
            observed.append(error)
            self.assertIn('HistoryLimitError', error)
            self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'armed')
            failing[0] = False
            self.fake.entries = [{'turnId': 'origin', 'item': {'type': 'commandExecution',
                                 'id': 'held', 'processId': '9', 'status': 'completed', 'exitCode': 0}}]
            self.fake.idle()
        with patch('codex_plus.background.MAX_PAGES', 2), patch('codex_plus.background.time.sleep', recover):
            self.observer.run()
        self.assertEqual(len(bounded_calls), 2)
        self.assertEqual(len(observed), 1)
        self.assertIsNone(self.store.status(PARENT)['parks'][0]['error'])
        self.assertEqual(self.fake.enqueues, 1)

    def test_registration_contender_waits_through_owner_exit(self):
        ctx = multiprocessing.get_context('fork')
        ready, release, gate = ctx.Event(), ctx.Event(), ctx.Event()
        owner = ctx.Process(target=exiting_owner, args=(self.temp.name, ready, release))
        owner.start()
        self.assertTrue(ready.wait(3))
        self.register()
        output = str(Path(self.temp.name) / 'starts')
        waiter = ctx.Process(target=contender, args=(self.temp.name, gate, output))
        waiter.start()
        gate.set()
        waiter.join(0.15)
        self.assertTrue(waiter.is_alive())
        release.set()
        owner.join(3)
        waiter.join(8)
        if waiter.is_alive():
            waiter.terminate()
        self.assertEqual(owner.exitcode, 0)
        self.assertEqual(waiter.exitcode, 0)
        self.assertEqual(Path(output).read_text(), 'started\n')

    def test_actual_process_concurrent_observers_single_event(self):
        self.register()
        ctx = multiprocessing.get_context('fork')
        gate = ctx.Event()
        output = str(Path(self.temp.name) / 'starts')
        children = [ctx.Process(target=contender, args=(self.temp.name, gate, output)) for _ in range(3)]
        for child in children:
            child.start()
        gate.set()
        for child in children:
            child.join(8)
            if child.is_alive():
                child.terminate()
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(Path(output).read_text(), 'started\n')
        self.assertEqual(self.callbacks()[0]['state'], 'delivered')


if __name__ == '__main__':
    unittest.main()
