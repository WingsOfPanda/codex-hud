"""Explicit, durable background completion callbacks for a loaded local parent."""
import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from .rpc import Client
from .ledger import MAX_SECONDS, TERMINAL, RegistrationError, Store, state_directory
from .evidence import terminal_event
from .upgrade import UPGRADE_EXIT, exec_package, package_command, package_env, select_package

MAX_PAGES = 32
HEARTBEAT_SECONDS = 5
SOURCES = ['subAgent', 'subAgentThreadSpawn', 'subAgentOther', 'subAgentReview', 'subAgentCompact']
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FAILED_PACKAGE = Path(os.environ['CODEX_PLUS_FAILED_PACKAGE']) if os.environ.get('CODEX_PLUS_FAILED_PACKAGE') else None


@contextmanager
def role_lock(home, parent, action):
    suffix = 'supervisor.lock' if action == 'supervise' else 'lock'
    fd = os.open(state_directory(home) / f'{parent}.{suffix}', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def check_upgrade(store, parent):
    try:
        candidate = select_package(PACKAGE_ROOT)
        if candidate is not None and candidate == FAILED_PACKAGE:
            raise ValueError('package exec previously failed')
    except ValueError:
        store.upgrade_warning(parent, True)
        return None
    store.upgrade_warning(parent, False)
    return candidate


class HistoryLimitError(ValueError):
    """History could not be fully reconciled within the local read bound."""


def pages(client, method, params):
    params = dict(params, limit=100)
    seen = set()
    for _ in range(MAX_PAGES):
        result = client.call(method, params)
        data = result.get('data')
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError(f'{method}: malformed data')
        yield from data
        cursor = result.get('nextCursor')
        if cursor is None:
            return
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError(f'{method}: invalid cursor')
        seen.add(cursor)
        params['cursor'] = cursor
    raise HistoryLimitError(f'{method}: exceeds {MAX_PAGES} pages; state remains unknown')


def latest(client, thread):
    data = client.call('thread/turns/list', {'threadId': thread, 'limit': 1,
                      'sortDirection': 'desc', 'itemsView': 'notLoaded'}).get('data')
    if not isinstance(data, list) or not data or not isinstance(data[0], dict) or not data[0].get('id'):
        raise ValueError(f'thread {thread} has no current turn')
    return data[0]


def turn(client, thread, ident):
    return next((t for t in pages(client, 'thread/turns/list',
                {'threadId': thread, 'itemsView': 'notLoaded', 'sortDirection': 'desc'})
                if t.get('id') == ident), None)


def items(client, thread, turn_id=None):
    params = {'threadId': thread, 'sortDirection': 'desc'}
    if turn_id:
        params['turnId'] = turn_id
    return pages(client, 'thread/items/list', params)


def state_home():
    return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))


def classify_turn(client, home, thread, current):
    status = current.get('status') if current else None
    if status != 'interrupted':
        return status, None
    completed = current.get('completedAt')
    if type(completed) in (int, float) and 0 < completed <= sys.float_info.max and math.isfinite(completed):
        return status, {'status': status, 'completedAt': completed, 'source': 'api'}
    # Stock reconstruction can project interruption before terminal history is visible.
    try:
        meta = client.call('thread/read', {'threadId': thread, 'includeTurns': False}).get('thread')
        if not isinstance(meta, dict) or meta.get('id') != thread or not meta.get('path'):
            return None, None
        event = terminal_event(home, meta['path'], thread, current['id'])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if event is None:
        return None, None
    evidence = {'status': event['status'], 'source': 'rollout', 'event': event['event']}
    if isinstance(event.get('timestamp'), str):
        evidence['timestamp'] = event['timestamp'][:64]
    return event['status'], evidence


def register(store, client, parent, workers, processes, reason):
    meta = client.call('thread/read', {'threadId': parent, 'includeTurns': False})['thread']
    if meta.get('id') != parent or meta.get('parentThreadId') or isinstance(meta.get('source'), dict) and 'subAgent' in meta['source']:
        raise RegistrationError('park requires a root parent thread')
    origin = latest(client, parent)
    if meta.get('status', {}).get('type') != 'active' or origin.get('status') != 'inProgress':
        raise RegistrationError('register park during the parent active turn, then end that turn')
    resolved = []
    pending = []
    labels = {}
    children = list(pages(client, 'thread/list', {'parentThreadId': parent,
                    'sourceKinds': SOURCES, 'useStateDbOnly': True})) if workers else []
    for selector in dict.fromkeys(workers):
        matches = []
        for child in children:
            source = child.get('source')
            agent = source.get('subAgent') if isinstance(source, dict) else None
            spawn = agent.get('thread_spawn') if isinstance(agent, dict) else None
            path = spawn.get('agent_path') if isinstance(spawn, dict) else None
            if child.get('parentThreadId') == parent and (child.get('id') == selector or path == selector):
                matches.append(child)
        if len(matches) != 1:
            raise RegistrationError(f'worker {selector!r} must uniquely match a direct child')
        child = matches[0]
        source = child.get('source')
        agent = source.get('subAgent') if isinstance(source, dict) else None
        spawn = agent.get('thread_spawn') if isinstance(agent, dict) else None
        labels[child['id']] = spawn.get('agent_path') if isinstance(spawn, dict) else None
        current = latest(client, child['id'])
        status, _ = classify_turn(client, store.directory.parent, child['id'], current)
        if current.get('status') == 'interrupted' and status is None:
            pending.append(f"{parent}:worker:{child['id']}:{current['id']}")
        resolved.append((f"{parent}:worker:{child['id']}:{current['id']}", 'worker',
                         child['id'], current['id'], None, None,
                         status if status in TERMINAL else 'armed', None))
    if processes:
        running = {p['processId']: p['itemId'] for p in pages(client, 'thread/backgroundTerminals/list', {'threadId': parent})}
        history = None
        for process in dict.fromkeys(processes):
            if process in running:
                # A running unified-exec begin is transient; terminal history supplies its exact turn.
                item_id = running[process]
                resolved.append((f'{parent}:process:item:{item_id}', 'process', parent,
                                 None, item_id, process, 'armed', None))
                continue
            if history is None:
                history = list(items(client, parent))
            candidates = [entry for entry in history if entry.get('item', {}).get('type') == 'commandExecution'
                          and entry['item'].get('processId') == process]
            if len(candidates) != 1:
                raise RegistrationError(f'process {process!r} must uniquely match a parent command item')
            entry = candidates[0]
            item = entry['item']
            resolved.append((f"{parent}:process:item:{item['id']}", 'process', parent,
                             entry['turnId'], item['id'], process, command_status(item), item.get('exitCode')))
    return store.register_resolved(parent, origin['id'], reason, resolved, labels, getattr(client, 'generation', None), pending)


def command_status(item):
    status = item.get('status')
    if status == 'completed':
        return 'completed' if item.get('exitCode') == 0 else 'failed' if isinstance(item.get('exitCode'), int) else 'armed'
    return {'failed': 'failed', 'declined': 'interrupted'}.get(status, 'armed')


class Observer:
    def __init__(self, store, parent, client_factory=None, fault=None):
        self.store, self.parent = store, parent
        self.client_factory = client_factory or (lambda: Client(state_home() / 'app-server-control' / 'app-server-control.sock'))
        self.client = None
        self.recovery_loaded = True
        self.run_id = str(uuid.uuid4())
        self.last_heartbeat = 0
        self.fault = fault or (lambda point: None)

    def call(self, method, params):
        if time.monotonic() - self.last_heartbeat >= HEARTBEAT_SECONDS:
            self.store.publish(self.parent)
            self.last_heartbeat = time.monotonic()
        if self.client is None:
            self.client = self.client_factory()
        return self.client.call(method, params)

    def recover(self):
        self.store.claim_observation(self.parent, self.run_id, recovering=True)

    def claim_jobs(self):
        self.store.claim_observation(self.parent, self.run_id)

    def recover_generation(self):
        if self.client is None:
            self.client = self.client_factory()
        replaced = self.store.observe_generation(self.parent, getattr(self.client, 'generation', None))
        if replaced:
            self.cancel_interrupted()
            if not self.store.active_parks(self.parent):
                return
            parent = self.call('thread/read', {'threadId': self.parent, 'includeTurns': False})['thread']
            if parent.get('id') != self.parent or parent.get('parentThreadId') or isinstance(parent.get('source'), dict) and 'subAgent' in parent['source']:
                raise ValueError('registered recovery parent is not a root thread')
            self.recovery_loaded = parent.get('status', {}).get('type') != 'notLoaded'
            if not self.recovery_loaded:
                queue = list(pages(self, 'thread/queue/list', {'threadId': self.parent}))
                self.cancel_interrupted()
                if not self.store.can_resume(self.parent, queue, self.native_queue()):
                    return
                self.call('thread/resume', {'threadId': self.parent, 'excludeTurns': True})
                self.recovery_loaded = True

    def cancel_interrupted(self):
        for park in self.store.active_parks(self.parent):
            anchor = turn(self, self.parent, park['origin'])
            status, evidence = classify_turn(self, self.store.directory.parent, self.parent, anchor)
            if status == 'interrupted':
                evidence.update(observer=self.run_id, generation=getattr(self.client, 'generation', None))
                self.store.request_cancel(self.parent, park['origin'], evidence=evidence)
                continue
            if anchor and anchor.get('status') == 'interrupted' and status is None:
                self.store.origin_pending(self.parent, park['origin'], True)
            elif status in ('completed', 'failed'):
                self.store.origin_pending(self.parent, park['origin'], False)
            if park['deadline'] < time.time():
                self.store.expire_registration(park['id'])

    def poll_jobs(self):
        jobs = self.store.jobs_to_observe(self.parent)
        running = {(p['itemId'], p['processId']) for p in pages(self, 'thread/backgroundTerminals/list', {'threadId': self.parent})} if any(j['kind'] == 'process' for j in jobs) else set()
        for job in jobs:
            if job['kind'] == 'worker':
                current = turn(self, job['thread'], job['turn'])
                status, _ = classify_turn(self, self.store.directory.parent, job['thread'], current)
                if (current and current.get('status') == 'interrupted' and status is None
                        or job['interruption_pending'] and status not in TERMINAL and status != 'inProgress'):
                    self.store.worker_pending(job['id'], True)
                    if not job['verify_running']:
                        continue
                    child = self.call('thread/read', {'threadId': job['thread'], 'includeTurns': False})['thread']
                    runtime = child.get('status', {}).get('type') if child.get('id') == job['thread'] else None
                    if runtime == 'active':
                        active = latest(self, job['thread'])
                        if active.get('id') == job['turn']:
                            if active.get('status') != 'inProgress':
                                continue
                            self.store.worker_pending(job['id'], False)
                            self.store.observe_job(job, running=True)
                            continue
                    elif runtime not in ('idle', 'notLoaded'):
                        continue
                    # A replaced daemon with no exact running task proves loss, not its outcome.
                    status = 'lost'
                self.store.worker_pending(job['id'], False)
                exit_code = None
                observed_running = status == 'inProgress'
                if observed_running and job['verify_running']:
                    child = self.call('thread/read', {'threadId': job['thread'], 'includeTurns': False})['thread']
                    current = latest(self, job['thread']) if child.get('id') == job['thread'] and child.get('status', {}).get('type') == 'active' else None
                    observed_running = bool(current and current.get('id') == job['turn'] and current.get('status') == 'inProgress')
            else:
                if (job['item'], job['process']) in running:
                    self.store.observe_job(job, running=True)
                    continue
                entry = next((e for e in items(self, job['thread'], job['turn']) if e.get('item', {}).get('id') == job['item']
                              and e['item'].get('processId') == job['process']), None)
                status = command_status(entry['item']) if entry else None
                exit_code = entry['item'].get('exitCode') if entry else None
                observed_running = entry is not None and status == 'armed' and not job['verify_running']
            self.store.observe_job(job, status, exit_code,
                                   entry['turnId'] if job['kind'] == 'process' and entry else None,
                                   running=observed_running)
        if self.recovery_loaded:
            self.store.finish_recovery(self.parent)

    def batch(self):
        self.store.batch(self.parent)

    def native_queue(self):
        return getattr(self.client, 'initialize_result', {}).get('queueIdempotency') is True

    def apply_receipt(self, callback, receipt):
        if not isinstance(receipt, dict) or receipt.get('receiptState') not in ('pending', 'consumed', 'cancelled'):
            raise ValueError('invalid callback receipt state')
        queued = receipt.get('queuedSubmission')
        if queued is None:
            if receipt['receiptState'] != 'cancelled':
                raise ValueError('callback receipt lacks submission identity')
        elif not isinstance(queued, dict) or queued.get('clientUserMessageId') != callback['client'] or not queued.get('id') or callback['queue'] and queued['id'] != callback['queue']:
            raise ValueError('callback receipt identity does not match')
        self.store.record_receipt(callback, receipt['receiptState'], queued['id'] if queued else None)

    def reconcile(self, callback, queue):
        cancelling = self.store.callback_cancelling(callback)
        if callback['idempotent'] and self.native_queue():
            result = self.call('thread/queue/receipt/cancel' if cancelling else 'thread/queue/receipt',
                               {'threadId': self.parent, 'idempotencyKey': callback['client']})
            if 'receipt' not in result or cancelling and result['receipt'] is None:
                raise ValueError('missing native callback receipt')
            if result['receipt'] is not None:
                self.apply_receipt(callback, result['receipt'])
            else:
                self.store.reconcile_evidence(callback, receipt_absent=True)
            return
        if callback['idempotent']:
            consumed = any(e.get('item', {}).get('type') == 'userMessage' and e['item'].get('clientId') == callback['client']
                           for e in items(self, self.parent))
            self.store.reconcile_evidence(callback, consumed=consumed, native_unavailable=True)
            return
        matches = [q for q in queue if q.get('clientUserMessageId') == callback['client']]
        if len(matches) > 1:
            raise ValueError('multiple queue items matched owned callback')
        consumed = False
        if not matches:
            consumed = any(e.get('item', {}).get('type') == 'userMessage' and e['item'].get('clientId') == callback['client']
                           for e in items(self, self.parent))
        deleted = None
        if matches and cancelling:
            result = self.call('thread/queue/delete', {'threadId': self.parent, 'queuedSubmissionId': matches[0]['id']})
            deleted = result.get('deleted')
        self.store.reconcile_evidence(callback, consumed=consumed,
                                      queued=matches[0] if matches else None, deleted=deleted)

    def deliver(self):
        queue = list(pages(self, 'thread/queue/list', {'threadId': self.parent}))
        callbacks = self.store.callbacks_to_reconcile(self.parent)
        for callback in callbacks:
            self.reconcile(callback, queue)
        parent = self.call('thread/read', {'threadId': self.parent, 'includeTurns': False})['thread']
        if parent.get('status', {}).get('type') != 'idle':
            return
        # Explicit selection can skip user entries; wait while any other queue item exists.
        if not self.store.queue_owned(self.parent, queue):
            return
        ready, blocked = self.store.next_callback(self.parent, native=self.native_queue())
        if blocked:
            return
        if ready:
            self.cancel_interrupted()
            ready = self.store.claim_callback(ready, native=self.native_queue())
            if ready is None:
                return
            message = ready['message']
            self.fault('before_enqueue')
            try:
                params = {'threadId': self.parent, 'clientUserMessageId': ready['client'],
                          'input': [{'type': 'text', 'text': message, 'textElements': []}]}
                if self.native_queue():
                    params['idempotencyKey'] = ready['client']
                result = self.call('thread/queue/add', params)
                self.fault('after_enqueue')
                queued = result['queuedSubmission']
                if queued.get('clientUserMessageId') != ready['client'] or not queued.get('id'):
                    raise ValueError('enqueue acknowledgement does not match callback')
                if self.native_queue():
                    self.apply_receipt(ready, result)
                else:
                    self.store.dispatch_result(ready, queued=queued['id'])
            except Exception:
                self.store.dispatch_result(ready)
                raise
        # Re-read queue and parent after enqueue; cancellation/active state may have raced.
        pending, _ = self.store.next_callback(self.parent, starting=True, native=self.native_queue())
        if not pending:
            return
        queue = list(pages(self, 'thread/queue/list', {'threadId': self.parent}))
        if not self.store.queue_owned(self.parent, queue) or not any(q.get('id') == pending['queue'] and q.get('clientUserMessageId') == pending['client'] for q in queue):
            return
        if self.call('thread/read', {'threadId': self.parent, 'includeTurns': False})['thread'].get('status', {}).get('type') != 'idle':
            return
        self.cancel_interrupted()
        claimed = self.store.claim_callback(pending, starting=True, native=self.native_queue())
        if claimed is None:
            return
        try:
            self.call('thread/queue/start', {'threadId': self.parent, 'queuedSubmissionId': pending['queue']})
            self.fault('after_start')
            if pending['idempotent']:
                # Started acknowledges dispatch; only the receipt proves durable input acceptance.
                self.reconcile(pending, [])
            else:
                self.store.dispatch_result(pending, starting=True, acknowledged=True)
        except Exception:
            self.store.dispatch_result(pending, starting=True)
            raise

    def step(self):
        self.claim_jobs()
        try:
            return self._step()
        finally:
            self.store.publish(self.parent)

    def _step(self):
        if not self.store.has_work(self.parent):
            return False
        self.recover_generation()
        self.cancel_interrupted()
        self.poll_jobs()
        self.batch()
        self.deliver()
        return self.store.finish_cycle(self.parent)

    def run(self, lock_held=False):
        with nullcontext() if lock_held else role_lock(self.store.directory.parent, self.parent, 'observe'):
            # Every registration launches a contender; a waiting contender rechecks durable work.
            if check_upgrade(self.store, self.parent):
                return UPGRADE_EXIT
            self.recover()
            self.store.publish(self.parent)
            backoff = 1
            deadline = time.monotonic() + MAX_SECONDS
            try:
                while time.monotonic() < deadline:
                    try:
                        active = self.step()
                        self.store.monitoring_succeeded(self.parent)
                        backoff = 1
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        if self.client:
                            self.client.close()
                        self.client = None
                        # Do not persist exception text: server errors can contain user content.
                        self.store.monitoring_failed(self.parent, type(exc).__name__)
                        self.store.publish(self.parent)
                        backoff = min(30, backoff * 2)
                        active = True
                    # Queue mutations and their acknowledgements have settled before selection.
                    if check_upgrade(self.store, self.parent):
                        return UPGRADE_EXIT
                    self.store.publish(self.parent)
                    if not active:
                        return
                    remaining = backoff
                    while remaining > 0:
                        delay = min(HEARTBEAT_SECONDS, remaining)
                        time.sleep(delay)
                        remaining -= delay
                        self.store.publish(self.parent)
            finally:
                if self.client:
                    self.client.close()
                self.store.publish(self.parent)
            self.store.expire_observation(self.parent)


def supervise(store, parent, home, lock_held=False):
    with nullcontext() if lock_held else role_lock(home, parent, 'supervise'):
        attempt = int(os.environ.get('CODEX_PLUS_SUPERVISOR_FAILURES', '0'))
        store.supervisor_failures = attempt
        if check_upgrade(store, parent):
            return UPGRADE_EXIT
        while attempt < 6:
            try:
                child = subprocess.Popen(package_command(PACKAGE_ROOT, 'observe', parent),
                                         cwd=PACKAGE_ROOT,
                                         env=dict(package_env(PACKAGE_ROOT, home), CODEX_PLUS_SUPERVISED='1',
                                                  CODEX_PLUS_FAILED_PACKAGE=str(FAILED_PACKAGE or '')), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                result = child.wait()
                if result == 0:
                    return
                if result == UPGRADE_EXIT:
                    if check_upgrade(store, parent):
                        store.supervisor_failures = attempt
                        return UPGRADE_EXIT
                    store.publish(parent)
                    continue
            except OSError:
                pass
            if attempt < 5:
                time.sleep(min(30, 2 ** attempt))
            attempt += 1
        store.supervisor_failed(parent)


def launch(parent, home):
    subprocess.Popen(package_command(PACKAGE_ROOT, 'supervise', parent),
                     cwd=PACKAGE_ROOT, env=package_env(PACKAGE_ROOT, home),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def main(argv=None):
    global FAILED_PACKAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['park', 'observe', 'supervise'])
    parser.add_argument('--parent', default=os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--worker', action='append', default=[])
    parser.add_argument('--process', action='append', default=[])
    parser.add_argument('--reason')
    parser.add_argument('--package-root', help=argparse.SUPPRESS)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--status', action='store_true')
    modes.add_argument('--cancel', action='store_true')
    args = parser.parse_args(argv)
    if args.package_root is not None and args.package_root != str(PACKAGE_ROOT) + '/':
        parser.error('--package-root must identify this package directory with a trailing slash')
    try:
        parent = str(uuid.UUID(args.parent or ''))
    except ValueError:
        parser.error('set CODEX_THREAD_ID or pass --parent with a thread UUID')
    if args.action == 'park' and not (args.status or args.cancel) and (not (args.worker or args.process) or not args.reason or len(args.reason) > 240 or not args.reason.isprintable()):
        parser.error('park needs explicit --worker/--process and a printable --reason of at most 240 characters')
    home = state_home()
    store = None
    try:
        if args.action != 'park' and (PACKAGE_ROOT / 'codex-plus.json').exists():
            os.chdir(PACKAGE_ROOT)
            if args.package_root is None:
                # Pin own source in cmdline while a later exec temporarily changes cwd.
                exec_package(PACKAGE_ROOT, args.action, parent, home,
                             list(argv) if argv is not None else sys.argv[1:])
        while True:
            # A blocked old contender must select again before opening or migrating the ledger.
            lock = role_lock(home, parent, args.action) if args.action != 'park' else nullcontext()
            with lock:
                invalid = False
                try:
                    candidate = select_package(PACKAGE_ROOT)
                    if candidate is not None and candidate == FAILED_PACKAGE:
                        raise ValueError('package exec previously failed')
                except ValueError:
                    candidate, invalid = None, True
                if candidate is None:
                    store = Store(home)
                    try:
                        store.upgrade_warning(parent, invalid)
                        if invalid and args.action == 'park':
                            print('codex-plus: coordinator upgrade unavailable; continuing installed helper.', file=sys.stderr)
                        if args.action == 'observe':
                            result = Observer(store, parent).run(lock_held=True)
                        elif args.action == 'supervise':
                            result = supervise(store, parent, home, lock_held=True)
                        else:
                            result = park(store, args, parent, home)
                            store.upgrade_warning(parent, invalid)
                            store.publish(parent)
                        if result != UPGRADE_EXIT:
                            return result or 0
                        if args.action == 'supervise':
                            os.environ['CODEX_PLUS_SUPERVISOR_FAILURES'] = str(store.supervisor_failures)
                        if args.action == 'observe' and os.environ.get('CODEX_PLUS_SUPERVISED') == '1':
                            return UPGRADE_EXIT
                    finally:
                        store.close()
                        store = None
            # Locks and SQLite are closed before exec; the new process rechecks under its lock.
            if candidate is None:
                try:
                    candidate = select_package(PACKAGE_ROOT)
                except ValueError:
                    continue
            if candidate is not None:
                if args.action == 'observe' and os.environ.get('CODEX_PLUS_SUPERVISED') == '1':
                    return UPGRADE_EXIT
                try:
                    exec_package(candidate, args.action, parent, home,
                                 list(argv) if argv is not None else sys.argv[1:])
                except OSError:
                    # Immutable packages are retried only in a new process or after current advances.
                    FAILED_PACKAGE = candidate
    except HistoryLimitError:
        print(f'codex-plus park: local history exceeds {MAX_PAGES} pages; selection remains unknown.', file=sys.stderr)
        return 1
    except RegistrationError as exc:
        print(f'codex-plus park: {exc}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f'codex-plus park failed ({type(exc).__name__}); inspect local daemon availability and selectors.', file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


def park(store, args, parent, home):
    if args.cancel:
        store.request_cancel(parent)
        launch(parent, home)
        print(json.dumps(store.status(parent)))
    elif args.status:
        print(json.dumps(store.status(parent)))
    else:
        client = Client(home / 'app-server-control' / 'app-server-control.sock')
        try:
            result = register(store, client, parent, args.worker, args.process, args.reason)
        finally:
            client.close()
        if result['state'] in ('armed', 'already_registered'):
            try:
                launch(parent, home)
            except OSError:
                store.launch_failed(parent)
                raise
            result['next'] = 'End the parent turn now. The local observer will wake it only after registered completion and actual Idle.'
        print(json.dumps(result))


if __name__ == '__main__':
    raise SystemExit(main())
