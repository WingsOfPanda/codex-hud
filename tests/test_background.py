"""Owned state and fake RPC lifecycle checks; no production queue writes."""
import fcntl
import json
import multiprocessing
import os
import socket
import signal
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import time

from codex_hud import background
from codex_hud.background import HistoryLimitError, Observer, Store, pages
from codex_hud.rpc import Client, peer_generation

PARENT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'


class Fake:
    def __init__(self):
        self.generation = 'boot:100:1'
        self.parent_status = 'active'
        self.parent_turns = [{'id': 'origin', 'status': 'inProgress'}]
        self.child_turns = [{'id': 'work', 'status': 'inProgress'}]
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

    def close(self):
        pass

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if self.fail_read and method not in ('thread/queue/add', 'thread/queue/start'):
            raise ConnectionError('disconnected')
        if method == 'thread/read':
            return {'thread': {'id': PARENT, 'source': 'cli', 'status': {'type': self.parent_status}}}
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


class Transport(unittest.TestCase):
    def test_linux_peer_generation_matches_process_and_changes_on_pid_reuse(self):
        first, second = socket.socketpair()
        try:
            generation = peer_generation(first)
            self.assertEqual(generation.split(':')[1], str(os.getpid()))
            self.assertEqual(generation, peer_generation(second))
            with patch('codex_hud.rpc.Path.read_text', side_effect=[
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
        return self.store.register(self.fake, PARENT, ['/root/work'], [], 'finish selected task')

    def callbacks(self):
        return self.store.status(PARENT)['callbacks']

    def test_worker_path_and_uuid_aliases_register_one_job(self):
        result = self.store.register(self.fake, PARENT, ['/root/work', CHILD], [], 'one worker')
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
        self.store.register(self.fake, PARENT, [], ['9'], 'held shell')
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
        self.fake.parent_turns[0]['status'] = 'interrupted'
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
        self.store.register(self.fake, PARENT, [], ['9'], 'same generation shell')
        self.fake.running = []
        self.fake.idle()
        self.assertTrue(self.observer.step())
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'armed')
        self.assertEqual(self.fake.enqueues, 0)

    def test_old_store_migration_preserves_jobs_without_assuming_generation_loss(self):
        self.register()
        with self.store.db:
            self.store.db.execute('ALTER TABLE parks DROP COLUMN generation')
            self.store.db.execute('ALTER TABLE parks DROP COLUMN recovery_generation')
            self.store.db.execute('ALTER TABLE callbacks DROP COLUMN reconcile_attempts')
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
        self.store.register(self.fake, PARENT, ['/root/work'], ['9'], 'two jobs')
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
        with patch('codex_hud.background.subprocess.Popen') as popen, patch('codex_hud.background.time.sleep'):
            popen.return_value.wait.side_effect = [-9, 79, 0]
            background.supervise(self.store, PARENT, self.temp.name)
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(popen.call_args[0][0][3:5], ['observe', '--parent'])

    def test_supervisor_crash_retries_are_bounded_and_durable(self):
        self.register()
        with patch('codex_hud.background.subprocess.Popen') as popen, patch('codex_hud.background.time.sleep'):
            popen.return_value.wait.return_value = 79
            background.supervise(self.store, PARENT, self.temp.name)
        self.assertEqual(popen.call_count, 6)
        self.assertIn('Supervisor restart limit', self.store.status(PARENT)['parks'][0]['error'])

    def test_actual_observer_kill_restarts_and_cancellation_stops_supervisor(self):
        self.register()
        supervisor = subprocess.Popen([sys.executable, '-m', 'codex_hud.background', 'supervise', '--parent', PARENT],
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
        self.store.register(self.fake, PARENT, [], ['42'], 'command result')
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
        self.fake.parent_turns[0]['status'] = 'interrupted'
        self.fake.parent_status = 'idle'
        self.fake.complete()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 0)
        self.assertEqual(self.store.status(PARENT)['parks'][0]['state'], 'cancelled')

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
        self.store.register(self.fake, PARENT, [], ['73'], 'held command')
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
        self.store.register(self.fake, PARENT, [], ['73'], 'same completed command')
        self.fake.idle()
        self.observer.step()
        self.assertEqual(self.fake.enqueues, 1)

    def test_coalesce_worker_and_command_terminal_same_poll(self):
        self.fake.running = [{'itemId': 'command', 'processId': '42'}]
        self.store.register(self.fake, PARENT, [CHILD], ['42'], 'finite batch')
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
        with self.store.db:
            self.store.db.execute("UPDATE callbacks SET state='in_flight'")
        self.store.request_cancel(PARENT)
        self.assertFalse(self.observer.step())
        self.assertEqual(self.callbacks()[0]['state'], 'cancel_uncertain')
        self.assertEqual(self.fake.enqueues, 0)

    def test_expired_work_never_fakes_completion(self):
        self.register()
        with self.store.db:
            self.store.db.execute('UPDATE parks SET deadline=0')
        self.fake.idle()
        self.fake.complete()
        self.assertFalse(self.observer.step())
        self.assertEqual(self.fake.enqueues, 0)
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['status'], 'expired')

    def test_interrupt_only_cancels_registration_from_that_origin(self):
        self.register()
        self.fake.parent_turns.insert(0, {'id': 'new-origin', 'status': 'inProgress'})
        self.fake.parent_turns[1]['status'] = 'interrupted'
        self.fake.child_turns = [{'id': 'new-work', 'status': 'inProgress'}]
        self.register()
        self.observer.step()
        states = {p['origin']: p['state'] for p in self.store.status(PARENT)['parks']}
        self.assertEqual(states, {'origin': 'cancelled', 'new-origin': 'armed'})

    def test_positive_running_registry_avoids_item_history(self):
        self.fake.running = [{'itemId': 'held', 'processId': '19'}]
        result = self.store.register(self.fake, PARENT, [], ['19'], 'held process')
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
        with self.store.db:
            self.store.db.execute("UPDATE parks SET state='expired'")
            self.store.db.execute("UPDATE jobs SET status='expired'")
        with self.assertRaisesRegex(ValueError, 'cannot be rearmed'):
            self.register()
        self.assertEqual(len(self.store.status(PARENT)['parks']), 1)

    def test_mixed_running_and_finished_registration_reports_both(self):
        self.fake.complete()
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        result = self.store.register(self.fake, PARENT, [CHILD], ['9'], 'mixed selections')
        self.assertEqual(result['state'], 'armed')
        self.assertEqual(len(result['armed']), 1)
        self.assertEqual(len(result['already_finished']), 1)
        self.assertEqual(self.store.status(PARENT)['jobs'][0]['kind'], 'process')

    def test_finished_process_needs_no_future_wake(self):
        self.fake.entries = [{'turnId': 'past', 'item': {'type': 'commandExecution',
                             'id': 'done', 'processId': '9', 'status': 'completed', 'exitCode': 0}}]
        result = self.store.register(self.fake, PARENT, [], ['9'], 'already finished')
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
                self.assertIn(self.store.register(self.fake, PARENT, [CHILD], [], 'ID selector')['state'],
                              ('armed', 'already_registered'))
                with self.assertRaisesRegex(ValueError, 'direct child'):
                    self.register()

    def test_pagination_bound_and_durable_sanitized_error_recovery(self):
        self.fake.running = [{'itemId': 'held', 'processId': '9'}]
        self.store.register(self.fake, PARENT, [], ['9'], 'history bound')
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
        with patch('codex_hud.background.MAX_PAGES', 2), patch('codex_hud.background.time.sleep', recover):
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
