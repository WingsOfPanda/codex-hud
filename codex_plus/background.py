"""Explicit, durable background completion callbacks for a loaded local parent."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from .rpc import Client

MAX_SECONDS = 24 * 60 * 60
MAX_PAGES = 32
TERMINAL = {'completed', 'failed', 'interrupted', 'lost'}
SOURCES = ['subAgent', 'subAgentThreadSpawn', 'subAgentOther', 'subAgentReview', 'subAgentCompact']


class RegistrationError(ValueError):
    """Safe, concrete selector/state failure for the CLI."""


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


class Store:
    def __init__(self, home):
        self.directory = Path(home) / 'codex-plus'
        if self.directory.is_symlink():
            raise ValueError('background directory must not be a symlink')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        path = self.directory / 'background.sqlite'
        # Refuse a substituted path before opening writable local state.
        if path.is_symlink():
            raise ValueError('background state must not be a symlink')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS parks (
                id TEXT PRIMARY KEY, parent TEXT NOT NULL, origin TEXT NOT NULL,
                reason TEXT NOT NULL, state TEXT NOT NULL, deadline REAL NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, park TEXT NOT NULL, kind TEXT NOT NULL,
                thread TEXT NOT NULL, turn TEXT, item TEXT, process TEXT,
                status TEXT NOT NULL, exit_code INTEGER, callback TEXT);
            CREATE TABLE IF NOT EXISTS callbacks (
                id TEXT PRIMARY KEY, parent TEXT NOT NULL, client TEXT UNIQUE NOT NULL,
                state TEXT NOT NULL, queue TEXT, error TEXT);
        ''')
        if 'error' not in {row[1] for row in self.db.execute('PRAGMA table_info(parks)')}:
            self.db.execute('ALTER TABLE parks ADD COLUMN error TEXT')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(parks)')}
        for column in ('generation', 'recovery_generation'):
            if column not in columns:
                self.db.execute(f'ALTER TABLE parks ADD COLUMN {column} TEXT')
        callback_columns = {row[1] for row in self.db.execute('PRAGMA table_info(callbacks)')}
        if 'reconcile_attempts' not in callback_columns:
            self.db.execute('ALTER TABLE callbacks ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0')
        if 'idempotent' not in callback_columns:
            self.db.execute('ALTER TABLE callbacks ADD COLUMN idempotent INTEGER NOT NULL DEFAULT 0')
        if 'message' not in callback_columns:
            self.db.execute('ALTER TABLE callbacks ADD COLUMN message TEXT')

    def close(self):
        self.db.close()

    def status(self, parent):
        parks = [dict(row) for row in self.db.execute('SELECT * FROM parks WHERE parent=?', (parent,))]
        jobs = [dict(row) for row in self.db.execute(
            'SELECT jobs.* FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=?', (parent,))]
        callbacks = [dict(row) for row in self.db.execute('SELECT * FROM callbacks WHERE parent=?', (parent,))]
        return {'parent': parent, 'parks': parks, 'jobs': jobs, 'callbacks': callbacks}

    def register(self, client, parent, workers, processes, reason):
        meta = client.call('thread/read', {'threadId': parent, 'includeTurns': False})['thread']
        if meta.get('id') != parent or meta.get('parentThreadId') or isinstance(meta.get('source'), dict) and 'subAgent' in meta['source']:
            raise RegistrationError('park requires a root parent thread')
        origin = latest(client, parent)
        if meta.get('status', {}).get('type') != 'active' or origin.get('status') != 'inProgress':
            raise RegistrationError('register park during the parent active turn, then end that turn')
        resolved = []
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
            current = latest(client, child['id'])
            resolved.append((f"{parent}:worker:{child['id']}:{current['id']}", 'worker',
                             child['id'], current['id'], None, None,
                             current['status'] if current.get('status') in TERMINAL else 'armed', None))
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
        resolved = list({job[0]: job for job in resolved}.values())
        armed, existing, finished = [], [], []
        park = None
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for job in resolved:
                prior = self.db.execute('''SELECT jobs.status,parks.state,callbacks.state AS delivery
                    FROM jobs JOIN parks ON parks.id=jobs.park
                    LEFT JOIN callbacks ON callbacks.id=jobs.callback WHERE jobs.id=?''', (job[0],)).fetchone()
                report = {'id': job[0], 'kind': job[1], 'turn': job[3], 'status': job[6]}
                if prior:
                    if prior['state'] in ('cancelled', 'expired') or prior['status'] in ('cancelled', 'expired'):
                        raise RegistrationError('Selected job was cancelled or expired; it cannot be rearmed. Select a new worker turn or command item.')
                    report.update(status=prior['status'], delivery=prior['delivery'])
                    if prior['delivery'] == 'delivered' or prior['state'] == 'complete':
                        finished.append(report)
                    else:
                        existing.append(report)
                elif job[6] in TERMINAL:
                    finished.append(report)
                else:
                    armed.append(job)
            if armed:
                park = str(uuid.uuid4())
                self.db.execute('INSERT INTO parks (id,parent,origin,reason,state,deadline,generation) VALUES (?,?,?,?,?,?,?)',
                                (park, parent, origin['id'], reason, 'armed', time.time() + MAX_SECONDS,
                                 getattr(client, 'generation', None)))
                for job in armed:
                    self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,NULL)', (job[0], park, *job[1:]))
        return {'parent': parent, 'park': park,
                'state': 'armed' if armed else 'already_registered' if existing else 'already_finished',
                'armed': [job[0] for job in armed], 'already_registered': existing, 'already_finished': finished}

    def request_cancel(self, parent, origin=None):
        scope = 'parent=?' + (' AND origin=?' if origin else '')
        params = (parent, origin) if origin else (parent,)
        with self.db:
            self.db.execute(f"UPDATE parks SET state='cancelled' WHERE {scope} AND state='armed'", params)
            self.db.execute(f"UPDATE jobs SET status='cancelled' WHERE status='armed' AND park IN (SELECT id FROM parks WHERE {scope})", params)
            self.db.execute(f"UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'cancelled' ELSE 'cancel_requested' END WHERE id IN (SELECT callback FROM jobs WHERE park IN (SELECT id FROM parks WHERE {scope})) AND state IN ('ready','in_flight','uncertain','pending','starting')", params)


def command_status(item):
    status = item.get('status')
    if status == 'completed':
        return 'completed' if item.get('exitCode') == 0 else 'failed' if isinstance(item.get('exitCode'), int) else 'armed'
    return {'failed': 'failed', 'declined': 'interrupted'}.get(status, 'armed')


class Observer:
    def __init__(self, store, parent, client_factory=None, fault=None):
        self.store, self.db, self.parent = store, store.db, parent
        self.client_factory = client_factory or (lambda: Client(state_home() / 'app-server-control' / 'app-server-control.sock'))
        self.client = None
        self.recovery_loaded = True
        self.fault = fault or (lambda point: None)

    def call(self, method, params):
        if self.client is None:
            self.client = self.client_factory()
        return self.client.call(method, params)

    def recover(self):
        with self.db:
            self.db.execute("UPDATE callbacks SET state='uncertain' WHERE parent=? AND state IN ('in_flight','starting')", (self.parent,))

    def recover_generation(self):
        if self.client is None:
            self.client = self.client_factory()
        generation = getattr(self.client, 'generation', None)
        if generation is None:
            return
        with self.db:
            # Old stores have no generation evidence; establish a baseline without assuming loss.
            self.db.execute("UPDATE parks SET generation=? WHERE parent=? AND state='armed' AND generation IS NULL",
                            (generation, self.parent))
            self.db.execute("UPDATE parks SET recovery_generation=? WHERE parent=? AND state='armed' AND generation<>?",
                            (generation, self.parent, generation))
        replaced = self.db.execute("SELECT 1 FROM parks WHERE parent=? AND state='armed' AND recovery_generation IS NOT NULL LIMIT 1", (self.parent,)).fetchone()
        if replaced:
            # Interrupted origins lack abort provenance; preserve cancellation conservatively.
            self.cancel_interrupted()
            if not self.db.execute("SELECT 1 FROM parks WHERE parent=? AND state='armed' LIMIT 1", (self.parent,)).fetchone():
                return
            parent = self.call('thread/read', {'threadId': self.parent, 'includeTurns': False})['thread']
            if parent.get('id') != self.parent or parent.get('parentThreadId') or isinstance(parent.get('source'), dict) and 'subAgent' in parent['source']:
                raise ValueError('registered recovery parent is not a root thread')
            self.recovery_loaded = parent.get('status', {}).get('type') != 'notLoaded'
            if not self.recovery_loaded:
                queue = list(pages(self, 'thread/queue/list', {'threadId': self.parent}))
                callbacks = {row['client']: row for row in self.db.execute('SELECT client,state,idempotent FROM callbacks WHERE parent=?', (self.parent,))}
                if any(q.get('clientUserMessageId') not in callbacks or callbacks[q['clientUserMessageId']]['state'] not in ('pending', 'uncertain', 'starting', 'in_flight') or callbacks[q['clientUserMessageId']]['idempotent'] and not self.native_queue() for q in queue):
                    return
                self.call('thread/resume', {'threadId': self.parent, 'excludeTurns': True})
                self.recovery_loaded = True

    def cancel_interrupted(self):
        for park in self.db.execute("SELECT * FROM parks WHERE parent=? AND state='armed'", (self.parent,)).fetchall():
            anchor = turn(self, self.parent, park['origin'])
            if anchor and anchor.get('status') == 'interrupted':
                self.store.request_cancel(self.parent, park['origin'])
                continue
            if park['deadline'] < time.time():
                with self.db:
                    self.db.execute("UPDATE parks SET state='expired' WHERE id=?", (park['id'],))
                    self.db.execute("UPDATE jobs SET status='expired' WHERE park=? AND status='armed'", (park['id'],))
                    self.db.execute("UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'expired' ELSE 'cancel_requested' END WHERE id IN (SELECT callback FROM jobs WHERE park=?) AND state IN ('ready','pending','starting','in_flight')", (park['id'],))

    def poll_jobs(self):
        jobs = self.db.execute("SELECT jobs.*,parks.recovery_generation FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND jobs.status='armed'", (self.parent,)).fetchall()
        running = {(p['itemId'], p['processId']) for p in pages(self, 'thread/backgroundTerminals/list', {'threadId': self.parent})} if any(j['kind'] == 'process' and not j['recovery_generation'] for j in jobs) else set()
        for job in jobs:
            if job['kind'] == 'worker':
                current = turn(self, job['thread'], job['turn'])
                status = current.get('status') if current else None
                exit_code = None
            else:
                if (job['item'], job['process']) in running and not job['recovery_generation']:
                    continue
                entry = next((e for e in items(self, job['thread'], job['turn']) if e.get('item', {}).get('id') == job['item']
                              and e['item'].get('processId') == job['process']), None)
                status = command_status(entry['item']) if entry else None
                exit_code = entry['item'].get('exitCode') if entry else None
            if status not in TERMINAL and job['recovery_generation']:
                status, exit_code = 'lost', None
            if status in TERMINAL:
                with self.db:
                    self.db.execute('UPDATE jobs SET status=?,exit_code=?,turn=COALESCE(turn,?) WHERE id=? AND status=\'armed\'', (status, exit_code, entry['turnId'] if job['kind'] == 'process' and entry else None, job['id']))
        if self.recovery_loaded:
            with self.db:
                self.db.execute("UPDATE parks SET generation=recovery_generation,recovery_generation=NULL WHERE parent=? AND state='armed' AND recovery_generation IS NOT NULL", (self.parent,))

    def batch(self):
        rows = self.db.execute("SELECT jobs.id,parks.origin FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND jobs.status IN ('completed','failed','interrupted','lost') AND jobs.callback IS NULL", (self.parent,)).fetchall()
        if not rows:
            return
        rows = [row for row in rows if row['origin'] == rows[0]['origin']]
        ident, client = str(uuid.uuid4()), str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO callbacks (id,parent,client,state,queue,error) VALUES (?,?,?,\'ready\',NULL,NULL)', (ident, self.parent, client))
            self.db.executemany('UPDATE jobs SET callback=? WHERE id=? AND callback IS NULL', [(ident, r['id']) for r in rows])

    def message(self, callback):
        rows = self.db.execute('SELECT jobs.*,parks.reason FROM jobs JOIN parks ON parks.id=jobs.park WHERE callback=?', (callback['id'],)).fetchall()
        remaining = self.db.execute("SELECT COUNT(*) FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND jobs.status='armed' AND parks.state='armed'", (self.parent,)).fetchone()[0]
        details = '; '.join(f"{r['kind']} {r['thread'] if r['kind']=='worker' else r['process']} turn={r['turn']} status={r['status']}" + (' outcome=UNKNOWN' if r['status'] == 'lost' else '') + (f" exit={r['exit_code']}" if r['exit_code'] is not None else '') for r in rows)
        reasons = '; '.join(dict.fromkeys(r['reason'] for r in rows))
        warning = 'Daemon generation changed; execution may have survived and its outcome is unknown. Do not rerun lost jobs automatically. ' if any(r['status'] == 'lost' for r in rows) else ''
        return (f'Background completion for your registered work: {reasons}. {details}. Remaining registered jobs: {remaining}. '
                + warning +
                'Review the results and continue the user task if still relevant. If only background work remains, park again and end your turn.')

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
        state = {'pending': 'pending', 'consumed': 'delivered', 'cancelled': 'cancelled'}[receipt['receiptState']]
        with self.db:
            self.db.execute("UPDATE callbacks SET state=CASE WHEN ?='pending' AND state IN ('cancel_requested','cancel_uncertain') THEN state ELSE ? END,queue=COALESCE(?,queue),error=NULL,reconcile_attempts=0 WHERE id=?",
                            (state, state, queued['id'] if queued else None, callback['id']))

    def reconcile(self, callback, queue):
        cancelling = callback['state'] in ('cancel_requested', 'cancel_uncertain') or bool(self.db.execute("SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE callback=? AND parks.state IN ('expired','cancelled') LIMIT 1", (callback['id'],)).fetchone())
        if callback['idempotent'] and self.native_queue():
            result = self.call('thread/queue/receipt/cancel' if cancelling else 'thread/queue/receipt',
                               {'threadId': self.parent, 'idempotencyKey': callback['client']})
            if 'receipt' not in result or cancelling and result['receipt'] is None:
                raise ValueError('missing native callback receipt')
            if result['receipt'] is not None:
                self.apply_receipt(callback, result['receipt'])
            else:
                # This key was durably armed before sending; absence proves enqueue never committed.
                with self.db:
                    self.db.execute("UPDATE callbacks SET state='ready',error=NULL,reconcile_attempts=0 WHERE id=? AND state IN ('uncertain','pending','starting')", (callback['id'],))
            return
        if callback['idempotent']:
            consumed = any(e.get('item', {}).get('type') == 'userMessage' and e['item'].get('clientId') == callback['client']
                           for e in items(self, self.parent))
            with self.db:
                if consumed:
                    self.db.execute("UPDATE callbacks SET state='delivered',error=NULL WHERE id=?", (callback['id'],))
                else:
                    self.db.execute("UPDATE callbacks SET state=CASE WHEN state='cancel_requested' THEN 'cancel_uncertain' ELSE state END,error='Keyed callback waits for native queue idempotency support.' WHERE id=?", (callback['id'],))
            return
        matches = [q for q in queue if q.get('clientUserMessageId') == callback['client']]
        if len(matches) > 1:
            raise ValueError('multiple queue items matched owned callback')
        consumed = False
        if not matches:
            consumed = any(e.get('item', {}).get('type') == 'userMessage' and e['item'].get('clientId') == callback['client']
                           for e in items(self, self.parent))
        with self.db:
            if consumed:
                self.db.execute("UPDATE callbacks SET state='delivered',error=NULL,reconcile_attempts=0 WHERE id=?", (callback['id'],))
            elif matches:
                if cancelling:
                    result = self.call('thread/queue/delete', {'threadId': self.parent, 'queuedSubmissionId': matches[0]['id']})
                    if result.get('deleted') is not True:
                        self.db.execute("UPDATE callbacks SET state='cancel_uncertain' WHERE id=?", (callback['id'],))
                        return
                    self.db.execute("UPDATE callbacks SET state='cancelled',queue=?,error=NULL WHERE id=?", (matches[0]['id'], callback['id']))
                else:
                    self.db.execute("UPDATE callbacks SET state='pending',queue=?,error=NULL,reconcile_attempts=0 WHERE id=?", (matches[0]['id'], callback['id']))
            else:
                self.db.execute('UPDATE callbacks SET state=?,error=?,reconcile_attempts=reconcile_attempts+1 WHERE id=?',
                                ('cancel_uncertain' if cancelling else 'uncertain', 'No positive queue or consumed-message evidence; enqueue will not be retried.', callback['id']))

    def deliver(self):
        queue = list(pages(self, 'thread/queue/list', {'threadId': self.parent}))
        callbacks = self.db.execute("SELECT * FROM callbacks WHERE parent=? AND state IN ('uncertain','pending','starting','cancel_requested','cancel_uncertain')", (self.parent,)).fetchall()
        for callback in callbacks:
            self.reconcile(callback, queue)
        parent = self.call('thread/read', {'threadId': self.parent, 'includeTurns': False})['thread']
        if parent.get('status', {}).get('type') != 'idle':
            return
        # Explicit selection can skip user entries; wait while any other queue item exists.
        own = {r['client'] for r in self.db.execute('SELECT client FROM callbacks WHERE parent=?', (self.parent,))}
        if any(q.get('clientUserMessageId') not in own for q in queue):
            return
        ready = self.db.execute("SELECT * FROM callbacks WHERE parent=? AND state='ready' LIMIT 1", (self.parent,)).fetchone()
        if ready:
            if ready['idempotent'] and not self.native_queue():
                with self.db:
                    self.db.execute("UPDATE callbacks SET error='Keyed callback waits for native queue idempotency support.' WHERE id=?", (ready['id'],))
                return
            self.cancel_interrupted()
            message = ready['message'] if ready['message'] is not None else self.message(ready)
            with self.db:
                claimed = self.db.execute("UPDATE callbacks SET state='in_flight',idempotent=MAX(idempotent,?),message=COALESCE(message,?) WHERE id=? AND state='ready'", (int(self.native_queue()), message, ready['id'])).rowcount
            if not claimed:
                return
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
                    with self.db:
                        self.db.execute("UPDATE callbacks SET state='pending',queue=? WHERE id=? AND state='in_flight'", (queued['id'], ready['id']))
            except Exception:
                with self.db:
                    self.db.execute("UPDATE callbacks SET state='uncertain' WHERE id=? AND state='in_flight'", (ready['id'],))
                raise
        # Re-read queue and parent after enqueue; cancellation/active state may have raced.
        pending = self.db.execute("SELECT * FROM callbacks WHERE parent=? AND state='pending' LIMIT 1", (self.parent,)).fetchone()
        if not pending or pending['idempotent'] and not self.native_queue():
            return
        queue = list(pages(self, 'thread/queue/list', {'threadId': self.parent}))
        if any(q.get('clientUserMessageId') not in own for q in queue) or not any(q.get('id') == pending['queue'] and q.get('clientUserMessageId') == pending['client'] for q in queue):
            return
        if self.call('thread/read', {'threadId': self.parent, 'includeTurns': False})['thread'].get('status', {}).get('type') != 'idle':
            return
        self.cancel_interrupted()
        with self.db:
            changed = self.db.execute("UPDATE callbacks SET state='starting' WHERE id=? AND state='pending'", (pending['id'],)).rowcount
        if not changed:
            return
        try:
            self.call('thread/queue/start', {'threadId': self.parent, 'queuedSubmissionId': pending['queue']})
            self.fault('after_start')
            if pending['idempotent']:
                # Started acknowledges dispatch; only the receipt proves durable input acceptance.
                self.reconcile(pending, [])
            else:
                with self.db:
                    self.db.execute("UPDATE callbacks SET state='delivered',error=NULL WHERE id=? AND state='starting'", (pending['id'],))
        except Exception:
            with self.db:
                self.db.execute("UPDATE callbacks SET state='uncertain' WHERE id=? AND state='starting'", (pending['id'],))
            raise

    def step(self):
        registered = self.db.execute("SELECT 1 FROM parks WHERE parent=? AND state='armed' LIMIT 1", (self.parent,)).fetchone()
        callbacks = self.db.execute("SELECT 1 FROM callbacks WHERE parent=? AND state IN ('ready','pending','starting','in_flight','uncertain','cancel_requested','cancel_uncertain') LIMIT 1", (self.parent,)).fetchone()
        if not registered and not callbacks:
            return False
        self.recover_generation()
        self.cancel_interrupted()
        self.poll_jobs()
        self.batch()
        self.deliver()
        with self.db:
            self.db.execute("UPDATE parks SET state='complete' WHERE parent=? AND state='armed' AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.park=parks.id AND (jobs.status='armed' OR (jobs.status IN ('completed','failed','interrupted','lost') AND (jobs.callback IS NULL OR jobs.callback IN (SELECT id FROM callbacks WHERE state NOT IN ('delivered','cancelled'))))))", (self.parent,))
        active = self.db.execute("SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND jobs.status='armed' LIMIT 1", (self.parent,)).fetchone()
        deliverable = self.db.execute("SELECT 1 FROM callbacks WHERE parent=? AND (state IN ('ready','pending','starting','in_flight','cancel_requested') OR (idempotent=1 AND state IN ('uncertain','cancel_uncertain')) OR (state='uncertain' AND queue IS NOT NULL AND reconcile_attempts<8)) LIMIT 1", (self.parent,)).fetchone()
        return bool(active or deliverable)

    def run(self):
        lock_path = self.store.directory / f'{self.parent}.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            # Every registration launches a contender; a waiting contender rechecks durable work.
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.recover()
            backoff = 1
            deadline = time.monotonic() + MAX_SECONDS
            try:
                while time.monotonic() < deadline:
                    try:
                        active = self.step()
                        with self.db:
                            self.db.execute('UPDATE parks SET error=NULL WHERE parent=?', (self.parent,))
                        if not active:
                            return
                        backoff = 1
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        if self.client:
                            self.client.close()
                        self.client = None
                        # Do not persist exception text: server errors can contain user content.
                        with self.db:
                            self.db.execute("UPDATE parks SET error=? WHERE parent=? AND state='armed'", (f'Monitoring unavailable ({type(exc).__name__}); completion remains unknown.', self.parent))
                            self.db.execute("UPDATE callbacks SET error=? WHERE parent=? AND state NOT IN ('delivered','cancelled')", (f'RPC unavailable ({type(exc).__name__}); no blind enqueue retry.', self.parent))
                        backoff = min(30, backoff * 2)
                    time.sleep(backoff)
            finally:
                if self.client:
                    self.client.close()
            with self.db:
                self.db.execute("UPDATE parks SET state='expired' WHERE parent=? AND state='armed'", (self.parent,))
                self.db.execute("UPDATE jobs SET status='expired' WHERE status='armed' AND park IN (SELECT id FROM parks WHERE parent=? AND state='expired')", (self.parent,))
                self.db.execute("UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'expired' ELSE 'uncertain' END,error='Observer duration expired; delivery needs positive reconciliation.' WHERE parent=? AND state IN ('ready','pending','starting','in_flight')", (self.parent,))


def supervise(store, parent, home):
    fd = os.open(store.directory / f'{parent}.supervisor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for attempt in range(6):
            try:
                child = subprocess.Popen([sys.executable, '-m', 'codex_plus.background', 'observe', '--parent', parent],
                                         cwd=Path(__file__).resolve().parent.parent,
                                         env=dict(os.environ, CODEX_HOME=str(home)), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                if child.wait() == 0:
                    return
            except OSError:
                pass
            if attempt < 5:
                time.sleep(min(30, 2 ** attempt))
        with store.db:
            store.db.execute("UPDATE parks SET error='Supervisor restart limit reached; observer recovery needs attention.' WHERE parent=? AND state='armed'", (parent,))


def launch(parent, home):
    subprocess.Popen([sys.executable, '-m', 'codex_plus.background', 'supervise', '--parent', parent],
                     cwd=Path(__file__).resolve().parent.parent, env=dict(os.environ, CODEX_HOME=str(home)),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['park', 'observe', 'supervise'])
    parser.add_argument('--parent', default=os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--worker', action='append', default=[])
    parser.add_argument('--process', action='append', default=[])
    parser.add_argument('--reason')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--status', action='store_true')
    modes.add_argument('--cancel', action='store_true')
    args = parser.parse_args(argv)
    try:
        parent = str(uuid.UUID(args.parent or ''))
    except ValueError:
        parser.error('set CODEX_THREAD_ID or pass --parent with a thread UUID')
    if args.action == 'park' and not (args.status or args.cancel) and (not (args.worker or args.process) or not args.reason or len(args.reason) > 240 or not args.reason.isprintable()):
        parser.error('park needs explicit --worker/--process and a printable --reason of at most 240 characters')
    home = state_home()
    store = Store(home)
    try:
        if args.action == 'observe':
            Observer(store, parent).run()
        elif args.action == 'supervise':
            supervise(store, parent, home)
        elif args.cancel:
            store.request_cancel(parent)
            launch(parent, home)
            print(json.dumps(store.status(parent)))
        elif args.status:
            print(json.dumps(store.status(parent)))
        else:
            client = Client(home / 'app-server-control' / 'app-server-control.sock')
            try:
                result = store.register(client, parent, args.worker, args.process, args.reason)
            finally:
                client.close()
            if result['state'] in ('armed', 'already_registered'):
                launch(parent, home)
                result['next'] = 'End the parent turn now. The local observer will wake it only after registered completion and actual Idle.'
            print(json.dumps(result))
        return 0
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
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
