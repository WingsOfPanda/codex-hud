"""Durable ownership and lifecycle policy for registered completion callbacks."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

MAX_SECONDS = 24 * 60 * 60
TERMINAL = {'completed', 'failed', 'interrupted', 'lost'}
UPGRADE_WARNING = 'Coordinator upgrade unavailable; continuing installed observer.'


def state_directory(home):
    directory = Path(home) / 'codex-plus'
    if directory.is_symlink():
        raise ValueError('background directory must not be a symlink')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


class RegistrationError(ValueError):
    """Safe, concrete selector/state failure for the CLI."""


class Store:
    def __init__(self, home):
        self.directory = state_directory(home)
        path = self.directory / 'background.sqlite'
        # Refuse a substituted path before opening writable local state.
        if path.is_symlink():
            raise ValueError('background state must not be a symlink')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self._db = sqlite3.connect(path, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.executescript('''
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
        self._db.execute('BEGIN IMMEDIATE')
        if 'error' not in {row[1] for row in self._db.execute('PRAGMA table_info(parks)')}:
            self._db.execute('ALTER TABLE parks ADD COLUMN error TEXT')
        columns = {row[1] for row in self._db.execute('PRAGMA table_info(parks)')}
        for column in ('generation', 'recovery_generation'):
            if column not in columns:
                self._db.execute(f'ALTER TABLE parks ADD COLUMN {column} TEXT')
        for column, definition in (('origin_pending', 'INTEGER NOT NULL DEFAULT 0'),
                                   ('cancelled_at', 'REAL'), ('cancel_kind', 'TEXT'), ('cancel_evidence', 'TEXT')):
            if column not in columns:
                self._db.execute(f'ALTER TABLE parks ADD COLUMN {column} {definition}')
        callback_columns = {row[1] for row in self._db.execute('PRAGMA table_info(callbacks)')}
        if 'reconcile_attempts' not in callback_columns:
            self._db.execute('ALTER TABLE callbacks ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0')
        if 'idempotent' not in callback_columns:
            self._db.execute('ALTER TABLE callbacks ADD COLUMN idempotent INTEGER NOT NULL DEFAULT 0')
        if 'message' not in callback_columns:
            self._db.execute('ALTER TABLE callbacks ADD COLUMN message TEXT')
        job_columns = {row[1] for row in self._db.execute('PRAGMA table_info(jobs)')}
        for column, definition in (('observer_run', 'TEXT'), ('recovery_pending', 'INTEGER NOT NULL DEFAULT 1'),
                                   ('report', 'INTEGER NOT NULL DEFAULT 0'), ('label', 'TEXT'),
                                   ('interruption_pending', 'INTEGER NOT NULL DEFAULT 0')):
            if column not in job_columns:
                self._db.execute(f'ALTER TABLE jobs ADD COLUMN {column} {definition}')
        self._db.commit()

    def close(self):
        self._db.close()

    def status(self, parent):
        parks = [dict(row) for row in self._db.execute('SELECT * FROM parks WHERE parent=?', (parent,))]
        jobs = [dict(row) for row in self._db.execute(
            'SELECT jobs.* FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=?', (parent,))]
        callbacks = [dict(row) for row in self._db.execute('SELECT * FROM callbacks WHERE parent=?', (parent,))]
        for park in parks:
            reason = ('Interrupted origin lacks positive terminal evidence; delivery and recovery resume are paused.'
                      if park['origin_pending'] else
                      'Interrupted worker lacks positive terminal evidence; its outcome remains pending.'
                      if any(j['park'] == park['id'] and j['interruption_pending'] for j in jobs) else None)
            park['pending_reason'] = reason if park['state'] == 'armed' else None
            if park['pending_reason']:
                park['monitoring_error'], park['error'] = park['error'], reason
        return {'parent': parent, 'parks': parks, 'jobs': jobs, 'callbacks': callbacks,
                'health': self.health(parent)}

    def health(self, parent, updated_at=None):
        # Expired registrations retain their warning but no longer contribute waiting jobs.
        pending = self._unsettled_job("'delivered','cancelled','expired'")
        jobs = self._db.execute(f"""SELECT jobs.report,jobs.status FROM jobs JOIN parks ON parks.id=jobs.park
            LEFT JOIN callbacks ON callbacks.id=jobs.callback WHERE parks.parent=? AND parks.state='armed'
            AND {pending}""", (parent,)).fetchall()
        uncertain = self._db.execute("SELECT COUNT(*) FROM callbacks WHERE parent=? AND state IN ('uncertain','cancel_uncertain')", (parent,)).fetchone()[0]
        error = self._db.execute("SELECT 1 FROM parks WHERE parent=? AND state<>'cancelled' AND (error IS NOT NULL OR origin_pending=1 OR EXISTS (SELECT 1 FROM jobs WHERE jobs.park=parks.id AND interruption_pending=1)) UNION ALL SELECT 1 FROM callbacks WHERE parent=? AND state NOT IN ('delivered','cancelled','expired') AND error IS NOT NULL LIMIT 1", (parent, parent)).fetchone()
        reports = sum(row['report'] for row in jobs)
        running = sum(row['status'] == 'armed' for row in jobs)
        state = 'uncertain' if uncertain else 'error' if error else 'waiting' if running else 'report' if reports else 'waiting' if jobs else 'idle'
        if updated_at is None:
            updated_at = 0
            try:
                fd = os.open(self.directory / f'{parent}.status.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        view = json.loads(stream.read(1024))
                        if view.get('parent') == parent and isinstance(view.get('updated_at'), (int, float)):
                            updated_at = view['updated_at']
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return {'version': 1, 'parent': parent, 'updated_at': updated_at,
                'state': state, 'jobs': running, 'uncertain': uncertain, 'reports': reports}

    def publish(self, parent):
        parent = str(uuid.UUID(parent))
        view = self.health(parent, time.time())
        temporary = self.directory / f'.{parent}.{uuid.uuid4()}.tmp'
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(view, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / f'{parent}.status.json')
        finally:
            temporary.unlink(missing_ok=True)
        return view

    def register_resolved(self, parent, origin, reason, resolved, labels, generation, pending=()):
        resolved = list({job[0]: job for job in resolved}.values())
        armed, existing, finished = [], [], []
        park = None
        with self._db:
            self._db.execute('BEGIN IMMEDIATE')
            for job in resolved:
                prior = self._db.execute('''SELECT jobs.status,parks.state,callbacks.state AS delivery
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
                self._db.execute('INSERT INTO parks (id,parent,origin,reason,state,deadline,generation) VALUES (?,?,?,?,?,?,?)',
                                (park, parent, origin, reason, 'armed', time.time() + MAX_SECONDS,
                                 generation))
                for job in armed:
                    label = (labels.get(job[2]) or job[2]) if job[1] == 'worker' else job[5]
                    label = ' '.join(str(label).split())[:96]
                    self._db.execute('''INSERT INTO jobs (id,park,kind,thread,turn,item,process,status,exit_code,callback,recovery_pending,label)
                        VALUES (?,?,?,?,?,?,?,?,?,NULL,0,?)''', (job[0], park, *job[1:], label))
                    if job[0] in pending:
                        self._db.execute('UPDATE jobs SET interruption_pending=1 WHERE id=?', (job[0],))
        self.publish(parent)
        return {'parent': parent, 'park': park,
                'state': 'armed' if armed else 'already_registered' if existing else 'already_finished',
                'armed': [job[0] for job in armed], 'already_registered': existing, 'already_finished': finished}

    def request_cancel(self, parent, origin=None, *, evidence=None):
        scope = 'parent=?' + (' AND origin=?' if origin else '')
        params = (parent, origin) if origin else (parent,)
        with self._db:
            self._db.execute('BEGIN IMMEDIATE')
            for park in self._db.execute(f"SELECT * FROM parks WHERE {scope} AND state<>'cancelled'", params).fetchall():
                provenance = {key: value[:160] if isinstance(value, str) else value
                              for key, value in (evidence or {'source': 'explicit'}).items()
                              if key in ('status', 'completedAt', 'source', 'event', 'timestamp', 'observer', 'generation')}
                owner = self._db.execute('SELECT observer_run FROM jobs WHERE park=? AND observer_run IS NOT NULL LIMIT 1', (park['id'],)).fetchone()
                provenance.setdefault('observer', owner['observer_run'] if owner else None)
                provenance.setdefault('generation', park['generation'])
                provenance['previous_error'] = park['error']
                self._db.execute("UPDATE parks SET state='cancelled',error=NULL,origin_pending=0,cancelled_at=?,cancel_kind=?,cancel_evidence=? WHERE id=?",
                                 (time.time(), 'confirmed_origin' if evidence else 'explicit',
                                  json.dumps(provenance, separators=(',', ':')), park['id']))
            self._db.execute(f"UPDATE jobs SET status='cancelled',interruption_pending=0 WHERE status='armed' AND park IN (SELECT id FROM parks WHERE {scope})", params)
            self._db.execute(f"UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'cancelled' ELSE 'cancel_requested' END WHERE id IN (SELECT callback FROM jobs WHERE park IN (SELECT id FROM parks WHERE {scope})) AND state IN ('ready','in_flight','uncertain','pending','starting')", params)
        self.publish(parent)

    def origin_pending(self, parent, origin, pending):
        with self._db:
            self._db.execute("UPDATE parks SET origin_pending=? WHERE parent=? AND origin=? AND state='armed'", (int(pending), parent, origin))

    def worker_pending(self, job, pending):
        with self._db:
            self._db.execute("UPDATE jobs SET interruption_pending=? WHERE id=? AND status='armed'", (int(pending), job))

    # A frozen callback owns its payload; only unclaimed work can change recovery class.
    _claimable = "park IN (SELECT id FROM parks WHERE parent=? AND state='armed') AND (status='armed' OR callback IS NULL OR callback IN (SELECT id FROM callbacks WHERE state='ready' AND message IS NULL))"
    _callback_open = "state IN ('ready','pending','starting','in_flight','uncertain','cancel_requested','cancel_uncertain')"
    _job_terminal = "jobs.status IN ('completed','failed','interrupted','lost')"

    def claim_observation(self, parent, run_id, recovering=False):
        with self._db:
            self._db.execute(f"UPDATE jobs SET recovery_pending=1 WHERE {self._claimable} AND observer_run IS NOT NULL AND observer_run<>?", (parent, run_id))
            self._db.execute(f"UPDATE jobs SET report=1 WHERE {self._claimable} AND recovery_pending=1 AND status IN ('completed','failed','interrupted','lost')", (parent,))
            self._db.execute(f'UPDATE jobs SET observer_run=? WHERE {self._claimable}', (run_id, parent))
            if recovering:
                self._db.execute("UPDATE callbacks SET state='uncertain' WHERE parent=? AND state IN ('in_flight','starting')", (parent,))

    def observe_generation(self, parent, generation):
        if generation is None:
            return False
        with self._db:
            # Missing generation evidence establishes a baseline without assuming loss.
            self._db.execute("UPDATE parks SET generation=? WHERE parent=? AND state='armed' AND generation IS NULL", (generation, parent))
            self._db.execute(f"""UPDATE jobs SET recovery_pending=1,report=CASE WHEN status IN ('completed','failed','interrupted','lost') THEN 1 ELSE report END
                WHERE {self._claimable} AND park IN (SELECT id FROM parks WHERE generation<>?
                AND (recovery_generation IS NULL OR recovery_generation<>?))""", (parent, generation, generation))
            self._db.execute("UPDATE parks SET recovery_generation=? WHERE parent=? AND state='armed' AND generation<>?", (generation, parent, generation))
        return bool(self._db.execute("SELECT 1 FROM parks WHERE parent=? AND state='armed' AND recovery_generation IS NOT NULL LIMIT 1", (parent,)).fetchone())

    def active_parks(self, parent):
        return self._db.execute("SELECT * FROM parks WHERE parent=? AND state='armed'", (parent,)).fetchall()

    def can_resume(self, parent, queue, native):
        if not self.active_parks(parent) or self._db.execute("SELECT 1 FROM parks WHERE parent=? AND state='armed' AND origin_pending=1 LIMIT 1", (parent,)).fetchone():
            return False
        callbacks = {row['client']: row for row in self._db.execute('SELECT client,state,idempotent FROM callbacks WHERE parent=?', (parent,))}
        return all(q.get('clientUserMessageId') in callbacks
                   and callbacks[q['clientUserMessageId']]['state'] in ('pending', 'uncertain', 'starting', 'in_flight')
                   and (not callbacks[q['clientUserMessageId']]['idempotent'] or native) for q in queue)

    def expire_registration(self, park):
        with self._db:
            self._db.execute("UPDATE parks SET state='expired',error='Registration duration expired; completion remains unknown.' WHERE id=? AND state='armed'", (park,))
            self._db.execute("UPDATE jobs SET status='expired',interruption_pending=0 WHERE park=? AND status='armed'", (park,))
            self._db.execute("UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'expired' ELSE 'cancel_requested' END WHERE id IN (SELECT callback FROM jobs WHERE park=?) AND state IN ('ready','pending','starting','in_flight')", (park,))

    def jobs_to_observe(self, parent):
        return self._db.execute("SELECT jobs.*,parks.recovery_generation IS NOT NULL AS verify_running FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND jobs.status='armed'", (parent,)).fetchall()

    def observe_job(self, job, status=None, exit_code=None, turn=None, running=False):
        with self._db:
            if running:
                self._db.execute("UPDATE jobs SET recovery_pending=0 WHERE id=? AND status='armed'", (job['id'],))
                return
            current = self._db.execute("SELECT jobs.status,parks.recovery_generation FROM jobs JOIN parks ON parks.id=jobs.park WHERE jobs.id=?", (job['id'],)).fetchone()
            if not current or current['status'] != 'armed':
                return
            if status not in TERMINAL and current['recovery_generation']:
                status, exit_code = 'lost', None
            if status in TERMINAL:
                self._db.execute("UPDATE jobs SET status=?,exit_code=?,turn=COALESCE(turn,?),report=recovery_pending,recovery_pending=0 WHERE id=? AND status='armed'", (status, exit_code, turn, job['id']))

    def finish_recovery(self, parent):
        with self._db:
            self._db.execute("""UPDATE parks SET generation=recovery_generation,recovery_generation=NULL
                WHERE parent=? AND state='armed' AND recovery_generation IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.park=parks.id AND status='armed' AND interruption_pending=1)""", (parent,))

    def batch(self, parent):
        with self._db:
            rows = self._db.execute(f"SELECT jobs.id,jobs.report,parks.origin FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND {self._job_terminal} AND jobs.callback IS NULL", (parent,)).fetchall()
            if not rows:
                return
            rows = [row for row in rows if (row['origin'], row['report']) == (rows[0]['origin'], rows[0]['report'])]
            ident, client = str(uuid.uuid4()), str(uuid.uuid4())
            self._db.execute("INSERT INTO callbacks (id,parent,client,state,queue,error) VALUES (?,?,?,'ready',NULL,NULL)", (ident, parent, client))
            self._db.executemany('UPDATE jobs SET callback=? WHERE id=? AND callback IS NULL', [(ident, r['id']) for r in rows])

    def _message(self, parent, callback):
        rows = self._db.execute('SELECT * FROM jobs WHERE callback=?', (callback['id'],)).fetchall()
        remaining = self._db.execute("SELECT COUNT(*) FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND jobs.status='armed' AND parks.state='armed'", (parent,)).fetchone()[0]
        details = []
        for row in rows[:3]:
            label = row['label'] or (row['thread'] if row['kind'] == 'worker' else row['process'])
            detail = f"{row['kind']} {' '.join(str(label).split())[:96]} status={row['status']}"
            if row['status'] == 'lost':
                detail += ' outcome=UNKNOWN'
            if row['exit_code'] is not None:
                detail += f" exit={row['exit_code']}"
            details.append(detail)
        details = '; '.join(details)
        if len(rows) > 3:
            details += f'; +{len(rows) - 3} more'
        report = any(r['report'] for r in rows)
        header = 'REPORT ONLY' if report else 'LIVE COMPLETION'
        warning = 'Do not resume earlier actions from this report. Do not rerun lost jobs. ' if report else ''
        return (f'{header}: {details}. Remaining registered jobs: {remaining}. {warning}'
                f'Results: codex-plus park --status --parent {parent}; callback={callback["id"]}.')

    def callbacks_to_reconcile(self, parent):
        return self._db.execute("SELECT * FROM callbacks WHERE parent=? AND state IN ('uncertain','pending','starting','cancel_requested','cancel_uncertain')", (parent,)).fetchall()

    def callback_cancelling(self, callback):
        return callback['state'] in ('cancel_requested', 'cancel_uncertain') or bool(self._db.execute("SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE callback=? AND parks.state IN ('expired','cancelled') LIMIT 1", (callback['id'],)).fetchone())

    def record_receipt(self, callback, receipt_state, queue):
        state = {'pending': 'pending', 'consumed': 'delivered', 'cancelled': 'cancelled'}[receipt_state]
        with self._db:
            self._db.execute("UPDATE callbacks SET state=CASE WHEN ?='pending' AND state IN ('cancel_requested','cancel_uncertain') THEN state ELSE ? END,queue=COALESCE(?,queue),error=NULL,reconcile_attempts=0 WHERE id=?", (state, state, queue, callback['id']))

    def reconcile_evidence(self, callback, *, consumed=False, queued=None, deleted=None, receipt_absent=False, native_unavailable=False):
        cancelling = self.callback_cancelling(callback)
        with self._db:
            if receipt_absent:
                self._db.execute("UPDATE callbacks SET state='ready',error=NULL,reconcile_attempts=0 WHERE id=? AND idempotent=1 AND state IN ('uncertain','pending','starting')", (callback['id'],))
            elif consumed:
                self._db.execute("UPDATE callbacks SET state='delivered',error=NULL,reconcile_attempts=CASE WHEN ? THEN reconcile_attempts ELSE 0 END WHERE id=?", (int(native_unavailable), callback['id']))
            elif native_unavailable:
                self._db.execute("UPDATE callbacks SET state=CASE WHEN state='cancel_requested' THEN 'cancel_uncertain' ELSE state END,error='Keyed callback waits for native queue idempotency support.' WHERE id=?", (callback['id'],))
            elif queued:
                if cancelling and deleted is not True:
                    self._db.execute("UPDATE callbacks SET state='cancel_uncertain' WHERE id=?", (callback['id'],))
                else:
                    self._db.execute("UPDATE callbacks SET state=?,queue=?,error=NULL,reconcile_attempts=CASE WHEN ? THEN reconcile_attempts ELSE 0 END WHERE id=?", ('cancelled' if cancelling else 'pending', queued['id'], int(cancelling), callback['id']))
            else:
                self._db.execute('UPDATE callbacks SET state=?,error=?,reconcile_attempts=reconcile_attempts+1 WHERE id=?', ('cancel_uncertain' if cancelling else 'uncertain', 'No positive queue or consumed-message evidence; enqueue will not be retried.', callback['id']))

    def queue_owned(self, parent, queue):
        own = {r['client'] for r in self._db.execute('SELECT client FROM callbacks WHERE parent=?', (parent,))}
        return all(q.get('clientUserMessageId') in own for q in queue)

    def next_callback(self, parent, *, starting=False, native=False):
        state = 'pending' if starting else 'ready'
        callback = self._db.execute('''SELECT * FROM callbacks WHERE parent=? AND state=?
            AND NOT EXISTS (SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park
                WHERE jobs.callback=callbacks.id AND parks.origin_pending=1) LIMIT 1''', (parent, state)).fetchone()
        if callback and callback['idempotent'] and not native:
            if not starting:
                with self._db:
                    self._db.execute("UPDATE callbacks SET error='Keyed callback waits for native queue idempotency support.' WHERE id=?", (callback['id'],))
            return None, not starting
        return callback, False

    def claim_callback(self, callback, *, starting=False, native=False):
        with self._db:
            self._db.execute('BEGIN IMMEDIATE')
            current = self._db.execute('SELECT * FROM callbacks WHERE id=?', (callback['id'],)).fetchone()
            expected = 'pending' if starting else 'ready'
            if not current or current['state'] != expected or self.callback_cancelling(current):
                return None
            if self._db.execute('''SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park
                WHERE jobs.callback=? AND parks.origin_pending=1 LIMIT 1''', (current['id'],)).fetchone():
                return None
            if current['idempotent'] and not native:
                return None
            if starting:
                self._db.execute("UPDATE callbacks SET state='starting' WHERE id=?", (current['id'],))
            else:
                message = current['message'] if current['message'] is not None else self._message(current['parent'], current)
                self._db.execute("UPDATE callbacks SET state='in_flight',idempotent=MAX(idempotent,?),message=COALESCE(message,?) WHERE id=?", (int(native), message, current['id']))
            return self._db.execute('SELECT * FROM callbacks WHERE id=?', (current['id'],)).fetchone()

    def dispatch_result(self, callback, *, starting=False, queued=None, acknowledged=False):
        expected = 'starting' if starting else 'in_flight'
        state = 'delivered' if starting and acknowledged else 'pending' if queued else 'uncertain'
        with self._db:
            if starting and acknowledged:
                self._db.execute("UPDATE callbacks SET state=?,error=NULL WHERE id=? AND state=? AND idempotent=0", (state, callback['id'], expected))
            else:
                self._db.execute('UPDATE callbacks SET state=?,queue=COALESCE(?,queue) WHERE id=? AND state=?', (state, queued, callback['id'], expected))

    def has_work(self, parent):
        return bool(self.active_parks(parent) or self._db.execute(f'SELECT 1 FROM callbacks WHERE parent=? AND {self._callback_open} LIMIT 1', (parent,)).fetchone())

    def _unsettled_job(self, final):
        return f"(jobs.status='armed' OR ({self._job_terminal} AND (jobs.callback IS NULL OR callbacks.state NOT IN ({final}))))"

    def finish_cycle(self, parent):
        pending = self._unsettled_job("'delivered','cancelled'")
        with self._db:
            self._db.execute(f"""UPDATE parks SET state='complete' WHERE parent=? AND state='armed'
                AND NOT EXISTS (SELECT 1 FROM jobs LEFT JOIN callbacks ON callbacks.id=jobs.callback
                WHERE jobs.park=parks.id AND {pending})""", (parent,))
        active = self._db.execute(f"SELECT 1 FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? AND parks.state='armed' AND (jobs.status='armed' OR ({self._job_terminal} AND jobs.callback IS NULL)) LIMIT 1", (parent,)).fetchone()
        deliverable = self._db.execute("SELECT 1 FROM callbacks WHERE parent=? AND (state IN ('ready','pending','starting','in_flight','cancel_requested') OR (idempotent=1 AND state IN ('uncertain','cancel_uncertain')) OR (state='uncertain' AND queue IS NOT NULL AND reconcile_attempts<8)) LIMIT 1", (parent,)).fetchone()
        return bool(active or deliverable)

    def monitoring_succeeded(self, parent):
        with self._db:
            # Successful monitoring does not resolve an active package-upgrade refusal.
            self._db.execute("""UPDATE parks SET error=NULL WHERE parent=? AND state NOT IN ('expired','cancelled')
                AND (state<>'armed' OR error IS NOT ?)
                AND origin_pending=0 AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.park=parks.id AND interruption_pending=1)""", (parent, UPGRADE_WARNING))
            # Legacy warning text is the on-disk provenance; keep its classification inside the ledger.
            self._db.execute("UPDATE callbacks SET error=NULL WHERE parent=? AND state='ready' AND error LIKE 'RPC unavailable (%); no blind enqueue retry.'", (parent,))

    def upgrade_warning(self, parent, invalid):
        with self._db:
            self._db.execute("UPDATE parks SET error=? WHERE parent=? AND state='armed' AND (error IS NULL OR error=?)",
                             (UPGRADE_WARNING if invalid else None, parent, UPGRADE_WARNING))

    def monitoring_failed(self, parent, error_type):
        with self._db:
            self._db.execute("UPDATE parks SET error=? WHERE parent=? AND state='armed'", (f'Monitoring unavailable ({error_type}); completion remains unknown.', parent))
            self._db.execute("UPDATE callbacks SET error=? WHERE parent=? AND state NOT IN ('delivered','cancelled')", (f'RPC unavailable ({error_type}); no blind enqueue retry.', parent))

    def supervisor_failed(self, parent):
        with self._db:
            self._db.execute("UPDATE parks SET error='Supervisor restart limit reached; observer recovery needs attention.' WHERE parent=? AND state='armed'", (parent,))
            self._db.execute("UPDATE callbacks SET error='Supervisor restart limit reached; observer recovery needs attention.' WHERE parent=? AND state NOT IN ('delivered','cancelled','expired')", (parent,))
        self.publish(parent)

    def launch_failed(self, parent):
        with self._db:
            self._db.execute("UPDATE parks SET error='Observer launch failed; monitoring unavailable.' WHERE parent=? AND state='armed'", (parent,))
        self.publish(parent)

    def expire_observation(self, parent):
        with self._db:
            self._db.execute("UPDATE parks SET state='expired',error='Observer duration expired; completion remains unknown.' WHERE parent=? AND state='armed'", (parent,))
            self._db.execute("UPDATE jobs SET status='expired',interruption_pending=0 WHERE status='armed' AND park IN (SELECT id FROM parks WHERE parent=? AND state='expired')", (parent,))
            self._db.execute("UPDATE callbacks SET state=CASE WHEN state='ready' THEN 'expired' ELSE 'uncertain' END,error='Observer duration expired; delivery needs positive reconciliation.' WHERE parent=? AND state IN ('ready','pending','starting','in_flight')", (parent,))
        self.publish(parent)
