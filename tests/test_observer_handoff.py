"""Private controller checks. Optional real-stock proof uses HANDOFF_* paths only."""
import copy
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('observer_handoff', REPO / 'scripts/handoff-observer.py')
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)
sys.path.insert(0, str(REPO))
from codex_plus.ledger import Store


def fixture_package(root):
    (root / 'codex_plus').mkdir(parents=True)
    files = {}
    for name in ('background.py', 'ledger.py', 'rpc.py'):
        path = root / 'codex_plus' / name
        path.write_text('# private fixture\n')
        files[f'codex_plus/{name}'] = {'size': path.stat().st_size,
            'mode': f'{path.stat().st_mode & 0o777:04o}', 'sha256': handoff.digest(path)}
    (root / 'codex-plus.json').write_text(json.dumps({'release': 'private', 'plus_commit': '0' * 40,
                                                    'files': files}))


class Controller(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='handoff-private-')
        self.root = Path(self.temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.package = self.root / 'package'
        fixture_package(self.package)
        self.parent = str(uuid.uuid4())
        self.store = Store(self.home)
        self.store.register_resolved(self.parent, 'origin', 'private',
            [('job', 'process', self.parent, None, 'item', 'process', 'armed', None)], {}, None)
        self.store.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_inspect_is_read_only_and_does_not_launch(self):
        database = self.home / 'codex-plus/background.sqlite'
        before = database.read_bytes()
        with patch.object(handoff, 'helpers', return_value=[]), \
                patch.object(handoff, 'root_idle') as rpc, \
                patch.object(handoff.subprocess, 'Popen') as launch, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(handoff.main(['--parent', self.parent, '--codex-home', str(self.home),
                                          '--package', str(self.package)]), 0)
        self.assertTrue(json.loads(output.getvalue())['needs_handoff'])
        self.assertEqual(database.read_bytes(), before)
        rpc.assert_not_called()
        launch.assert_not_called()

    def test_frozen_identity_pending_and_cancellation_survive(self):
        before = handoff.snapshot(self.home, self.parent)
        before['parks'][0].update(origin_pending=1)
        before['callbacks'] = [{'id': 'callback', 'parent': self.parent, 'client': 'client',
                                'message': 'frozen payload', 'idempotent': 0, 'state': 'uncertain'}]
        after = copy.deepcopy(before)
        after['callbacks'][0].update(state='delivered', queue='accepted')
        after['jobs'][0]['turn'] = 'resolved-turn'
        handoff.retained(before, after)
        for key, value in (('message', 'changed'), ('client', 'new'), ('idempotent', 1)):
            changed = copy.deepcopy(after)
            changed['callbacks'][0][key] = value
            with self.assertRaises(ValueError):
                handoff.retained(before, changed)
        before['jobs'][0]['status'] = 'cancelled'
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            handoff.retained(before, after)
        after = copy.deepcopy(before)
        after['parks'].append(dict(after['parks'][0], id='new-registration'))
        with self.assertRaisesRegex(ValueError, 'new registration'):
            handoff.retained(before, after)

    def test_active_or_foreign_parent_refused_by_read_rpc(self):
        class Client:
            generation = 'same'
            def __init__(self, *args, **kwargs):
                pass
            def call(client, method, params):
                self.assertEqual(method, 'thread/read')
                return {'thread': metadata}
            def close(self):
                pass
        module = type('RpcModule', (), {'Client': Client})
        for metadata in ({'id': self.parent, 'status': {'type': 'active'}},
                         {'id': self.parent, 'parentThreadId': 'other', 'status': {'type': 'idle'}},
                         {'id': self.parent, 'status': {'type': 'notLoaded'}}):
            with patch.object(handoff.importlib.util, 'module_from_spec', return_value=module), \
                    patch('importlib.machinery.SourceFileLoader.exec_module'), \
                    self.assertRaisesRegex(ValueError, 'idle root'):
                handoff.root_idle(self.package, self.home, self.parent)

    def test_pid_reuse_refuses_signal(self):
        identity = {'pid': os.getpid(), 'start_ticks': 'old'}
        with patch.object(handoff, 'process_identity', return_value=dict(identity, start_ticks='new')), \
                patch.object(handoff.signal, 'pidfd_send_signal') as send, \
                self.assertRaisesRegex(ValueError, 'identity changed'):
            handoff.stop_helper(identity, self.parent, self.home)
        send.assert_not_called()

    def test_selected_foreign_pythonpath_refused_and_other_parent_ignored(self):
        proc = self.root / 'proc'
        directory = proc / '1234'
        directory.mkdir(parents=True)
        (directory / 'cmdline').write_bytes(b'python3\0-m\0codex_plus.background\0observe\0--parent\0' + self.parent.encode() + b'\0')
        (directory / 'environ').write_bytes(f'CODEX_HOME={self.home}\0PYTHONPATH=/foreign\0'.encode())
        (directory / 'cwd').symlink_to(self.package)
        (directory / 'exe').symlink_to(Path(sys.executable).resolve())
        with self.assertRaisesRegex(ValueError, 'PYTHONPATH'):
            handoff.process_identity(1234, self.parent, self.home, proc)
        self.assertIsNone(handoff.process_identity(1234, str(uuid.uuid4()), self.home, proc))
        self.assertIsNone(handoff.process_identity(1234, self.parent, self.root, proc))

    def test_unrecognized_selected_python_argv_refused_before_any_signal(self):
        proc = self.root / 'proc'
        directory = proc / '1234'
        directory.mkdir(parents=True)
        (directory / 'environ').write_bytes(
            f'CODEX_HOME={self.home}\0CODEX_THREAD_ID={self.parent}\0'.encode())
        for command in (
            ['python3', '-u', '-m', 'codex_plus.background', 'supervise', '--parent', self.parent],
            ['python3', '-I', '-m', 'codex_plus.background', 'supervise', '--parent', self.parent],
            ['python3', '-m', 'codex_plus.background', 'supervise', f'--parent={self.parent}'],
            ['python3', '-m', 'codex_plus.background', 'supervise'],
        ):
            with self.subTest(command=command):
                (directory / 'cmdline').write_bytes(b'\0'.join(os.fsencode(part) for part in command) + b'\0')
                with patch.object(handoff, 'helpers', side_effect=lambda parent, home:
                        handoff.process_identity(1234, parent, home, proc)), \
                        patch.object(handoff, 'stop_helper') as stop, \
                        patch.object(handoff.subprocess, 'Popen') as launch, \
                        patch('sys.stderr', new_callable=io.StringIO) as output:
                    self.assertEqual(handoff.main(['--handoff', '--parent', self.parent,
                        '--codex-home', str(self.home), '--package', str(self.package),
                        '--evidence-dir', str(self.root / 'audit')]), 1)
                self.assertIn('ambiguous selected helper argv', output.getvalue())
                stop.assert_not_called()
                launch.assert_not_called()

    def test_selected_missing_manifest_refused_instead_of_ignored(self):
        proc = self.root / 'proc'
        directory = proc / '1234'
        directory.mkdir(parents=True)
        dev = self.root / 'unmanifested-checkout'
        dev.mkdir()
        (directory / 'cmdline').write_bytes(
            f'python3\0-m\0codex_plus.background\0supervise\0--parent\0{self.parent}\0'.encode())
        (directory / 'environ').write_bytes(f'CODEX_HOME={self.home}\0PYTHONPATH={dev}\0'.encode())
        (directory / 'cwd').symlink_to(dev)
        (directory / 'exe').symlink_to(Path(sys.executable).resolve())
        original = handoff.process_identity
        with patch.object(handoff, 'process_identity', side_effect=lambda pid, parent, home:
                original(1234, parent, home, proc)), patch.object(handoff, 'stop_helper') as stop, \
                self.assertRaisesRegex(ValueError, 'selected helper runtime is unavailable'):
            handoff.helpers(self.parent, self.home)
        stop.assert_not_called()

    def test_other_root_registration_allowed_selected_registration_refused(self):
        before = handoff.snapshot(self.home, self.parent)
        store = Store(self.home)
        try:
            other = str(uuid.uuid4())
            store.register_resolved(other, 'other-origin', 'other root',
                [('other-job', 'process', other, None, 'other-item', 'other-process', 'armed', None)], {}, None)
            after = handoff.snapshot(self.home, self.parent)
            handoff.retained(before, after)
            self.assertEqual(after, before)
            store.register_resolved(self.parent, 'new-origin', 'selected new registration',
                [('selected-new-job', 'process', self.parent, None, 'new-item', 'new-process', 'armed', None)], {}, None)
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, 'new registration'):
            handoff.retained(before, handoff.snapshot(self.home, self.parent))

    def test_stops_supervisor_before_observer_and_backs_up_before_launch(self):
        args = type('Args', (), {'package': self.package, 'codex_home': self.home,
                                'parent': self.parent, 'evidence_dir': self.root / 'audit'})
        supervisor = {'pid': 111, 'action': 'supervise', 'cwd': str(self.package)}
        observer = {'pid': 222, 'ppid': 333, 'action': 'observe', 'cwd': str(self.package)}
        order = []
        def stopped(identity, *unused):
            order.append(identity['action'])
        def launch(*unused, **kwargs):
            self.assertTrue((args.evidence_dir / 'ledger.sqlite').exists())
            self.assertEqual(kwargs['cwd'], self.package)
            self.assertEqual(kwargs['env']['PYTHONPATH'], str(self.package))
            self.assertTrue(kwargs['start_new_session'])
            order.append('launch')
            return type('Child', (), {'pid': 111, 'poll': lambda child: None})()
        with patch.object(handoff, 'helpers', side_effect=[[supervisor, observer], [observer], [],
                                                          [supervisor, dict(observer, ppid=111)]]), \
                patch.object(handoff, 'root_idle', return_value='daemon'), \
                patch.object(handoff, 'observer_lock_owner', side_effect=[222, None, 222]), \
                patch.object(handoff, 'stop_helper', side_effect=stopped), \
                patch.object(handoff.subprocess, 'Popen', side_effect=launch), \
                patch.object(handoff, 'health', side_effect=lambda *unused: {
                    'updated_at': time.time(), 'state': 'waiting'}):
            result = handoff.handoff(args, handoff.snapshot(self.home, self.parent), [supervisor, observer])
        self.assertEqual(order, ['supervise', 'observe', 'launch'])
        self.assertEqual(result['phase'], 'verified')
        self.assertEqual((args.evidence_dir.stat().st_mode & 0o777), 0o700)
        with sqlite3.connect(args.evidence_dir / 'ledger.sqlite') as database:
            self.assertEqual(database.execute('SELECT id FROM jobs').fetchone()[0], 'job')

    def test_unrecognized_real_lock_owner_refused_before_signals(self):
        args = type('Args', (), {'package': self.package, 'codex_home': self.home,
                                'parent': self.parent, 'evidence_dir': self.root / 'audit'})
        lock_path = self.home / 'codex-plus' / f'{self.parent}.lock'
        with lock_path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(handoff.observer_lock_owner(self.home, self.parent), os.getpid())
            with patch.object(handoff, 'root_idle', return_value='daemon'), \
                    patch.object(handoff, 'stop_helper') as stop, \
                    patch.object(handoff.subprocess, 'Popen') as launch, \
                    self.assertRaisesRegex(ValueError, 'unrecognized owner'):
                handoff.handoff(args, handoff.snapshot(self.home, self.parent), [])
        stop.assert_not_called()
        launch.assert_not_called()

    def test_new_lock_owner_spawned_after_process_scan_is_verified_directly(self):
        args = type('Args', (), {'package': self.package, 'codex_home': self.home,
                                'parent': self.parent, 'evidence_dir': self.root / 'audit'})
        supervisor = {'pid': 111, 'action': 'supervise', 'cwd': str(self.package)}
        observer = {'pid': 222, 'ppid': 111, 'action': 'observe', 'cwd': str(self.package),
                    'executable': str(Path(sys.executable).resolve()), 'start_ticks': 'new'}
        child = type('Child', (), {'pid': 111, 'poll': lambda child: None})()
        with patch.object(handoff, 'helpers', side_effect=[[], [], [], [supervisor]]), \
                patch.object(handoff, 'root_idle', return_value='daemon'), \
                patch.object(handoff, 'observer_lock_owner', side_effect=[None, None, 222]), \
                patch.object(handoff, 'process_identity', return_value=observer) as identify, \
                patch.object(handoff.os, 'pidfd_open', return_value=999), \
                patch.object(handoff.os, 'close'), \
                patch.object(handoff.select, 'select', return_value=([], [], [])), \
                patch.object(handoff.subprocess, 'Popen', return_value=child), \
                patch.object(handoff, 'health', side_effect=lambda *unused: {
                    'updated_at': time.time(), 'state': 'waiting'}):
            result = handoff.handoff(args, handoff.snapshot(self.home, self.parent), [])
        self.assertEqual(result['new_observer_lock_owner'], 222)
        self.assertIn(observer, result['new_helpers'])
        identify.assert_called_with(222, self.parent, self.home)

    def test_direct_lock_owner_probe_distinguishes_exit_from_foreign_process(self):
        with patch.object(handoff.os, 'pidfd_open', return_value=999), \
                patch.object(handoff.os, 'close'), \
                patch.object(handoff, 'process_identity', return_value=None), \
                patch.object(handoff.select, 'select', return_value=([999], [], [])):
            self.assertIsNone(handoff.live_helper(222, self.parent, self.home))
        with patch.object(handoff.os, 'pidfd_open', return_value=999), \
                patch.object(handoff.os, 'close'), \
                patch.object(handoff, 'process_identity', return_value=None), \
                patch.object(handoff.select, 'select', return_value=([], [], [])), \
                self.assertRaisesRegex(ValueError, 'unrecognized owner'):
            handoff.live_helper(222, self.parent, self.home)

    def test_killed_sqlite_writer_recovered_before_snapshot_and_backup(self):
        database_path = self.home / 'codex-plus/background.sqlite'
        with sqlite3.connect(database_path) as database:
            database.execute('CREATE TABLE spill (body BLOB)')
            database.executemany('INSERT INTO spill VALUES (?)', [(b'x' * 4096,)] * 128)
        before = handoff.snapshot(self.home, self.parent)
        source = '''import sqlite3,sys,time
database=sqlite3.connect(sys.argv[1])
database.execute('PRAGMA cache_size=1')
database.execute('BEGIN IMMEDIATE')
database.execute('UPDATE spill SET body=zeroblob(4096)')
print('transaction spilled',flush=True)
time.sleep(30)
'''
        writer = subprocess.Popen([sys.executable, '-c', source, str(database_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([writer.stdout], [], [], 5)[0])
            self.assertEqual(writer.stdout.readline().strip(), 'transaction spilled')
            args = type('Args', (), {'package': self.package, 'codex_home': self.home,
                                    'parent': self.parent, 'evidence_dir': self.root / 'audit'})
            old = {'pid': writer.pid, 'action': 'observe', 'cwd': str(self.package)}
            supervisor = {'pid': 111, 'action': 'supervise', 'cwd': str(self.package)}
            observer = {'pid': 222, 'ppid': 111, 'action': 'observe', 'cwd': str(self.package)}
            child = type('Child', (), {'pid': 111, 'poll': lambda child: None})()
            def stop(*unused):
                writer.kill()
                writer.wait(timeout=5)
                with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                    handoff.snapshot(self.home, self.parent)
            with patch.object(handoff, 'helpers', side_effect=[[old], [old], [], [supervisor, observer]]), \
                    patch.object(handoff, 'root_idle', return_value='daemon'), \
                    patch.object(handoff, 'observer_lock_owner', side_effect=[writer.pid, None, 222]), \
                    patch.object(handoff, 'stop_helper', side_effect=stop), \
                    patch.object(handoff.subprocess, 'Popen', return_value=child), \
                    patch.object(handoff, 'health', side_effect=lambda *unused: {
                        'updated_at': time.time(), 'state': 'waiting'}):
                result = handoff.handoff(args, before, [old])
            self.assertEqual(result['phase'], 'verified')
            self.assertEqual(handoff.snapshot(self.home, self.parent), before)
            with sqlite3.connect(args.evidence_dir / 'ledger.sqlite') as backup:
                self.assertEqual(backup.execute('SELECT body FROM spill LIMIT 1').fetchone()[0], b'x' * 4096)
        finally:
            if writer.poll() is None:
                writer.kill()
            writer.wait(timeout=5)
            writer.stdout.close()
            writer.stderr.close()

    def test_failed_handoff_never_restores_or_rearms(self):
        before = handoff.snapshot(self.home, self.parent)
        args = type('Args', (), {'package': self.package, 'codex_home': self.home,
                                'parent': self.parent, 'evidence_dir': self.root / 'audit'})
        with patch.object(handoff, 'root_idle', side_effect=ValueError('active')), \
                self.assertRaisesRegex(ValueError, 'active'):
            handoff.handoff(args, before, [])
        self.assertEqual(handoff.snapshot(self.home, self.parent), before)
        self.assertEqual(json.loads((args.evidence_dir / 'result.json').read_text())['phase'], 'preflight')

    def test_no_work_does_not_stop_or_launch(self):
        empty = self.root / 'empty'
        empty.mkdir()
        with patch.object(handoff, 'helpers', return_value=[]), \
                patch.object(handoff, 'handoff') as change, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(handoff.main(['--handoff', '--parent', self.parent, '--codex-home', str(empty),
                                          '--package', str(self.package)]), 0)
        change.assert_not_called()
        self.assertIn('no handoff needed', json.loads(output.getvalue())['outcome'])

    def test_existing_helpers_without_pending_work_still_handed_off(self):
        empty = {'parks': [], 'jobs': [], 'callbacks': []}
        selected = [{'pid': 1234, 'action': 'supervise'}]
        with patch.object(handoff, 'snapshot', return_value=empty), \
                patch.object(handoff, 'helpers', return_value=selected), \
                patch.object(handoff, 'handoff', return_value={'phase': 'verified'}) as change, \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(handoff.main(['--handoff', '--parent', self.parent,
                '--codex-home', str(self.home), '--package', str(self.package),
                '--evidence-dir', str(self.root / 'audit')]), 0)
        change.assert_called_once()
        self.assertEqual(change.call_args.args[1:], (empty, selected))

    def test_modified_or_unlisted_package_code_refused(self):
        (self.package / 'codex_plus/unlisted.py').write_text('unexpected')
        with self.assertRaisesRegex(ValueError, 'unlisted'):
            handoff.package_identity(self.package, verify=True)
        (self.package / 'codex_plus/unlisted.py').unlink()
        (self.package / 'codex_plus/background.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            handoff.package_identity(self.package)

    def test_installed_write_bits_removed_modes_accepted_other_changes_refused(self):
        binary = self.package / 'bin/codex'
        binary.parent.mkdir()
        binary.write_text('private binary fixture\n')
        binary.chmod(0o755)
        manifest_path = self.package / 'codex-plus.json'
        manifest = json.loads(manifest_path.read_text())
        for expected in manifest['files'].values():
            expected['mode'] = '0644'
        manifest['files']['bin/codex'] = {'size': binary.stat().st_size, 'mode': '0755',
                                        'sha256': handoff.digest(binary)}
        manifest_path.write_text(json.dumps(manifest))
        for name, expected in manifest['files'].items():
            (self.package / name).chmod(int(expected['mode'], 8) & ~0o222)
        manifest_path.chmod(0o444)
        handoff.package_identity(self.package, verify=True)
        binary.chmod(0o545)
        with self.assertRaisesRegex(ValueError, 'mismatch: bin/codex'):
            handoff.package_identity(self.package, verify=True)
        binary.chmod(0o555)
        (self.package / 'codex_plus/background.py').chmod(0o400)
        with self.assertRaisesRegex(ValueError, 'mismatch: codex_plus/background.py'):
            handoff.package_identity(self.package)


@unittest.skipUnless(all(os.environ.get(name) for name in
    ('HANDOFF_OLD_PACKAGE', 'HANDOFF_NEW_PACKAGE', 'HANDOFF_OFFICIAL_DAEMON', 'HANDOFF_EVIDENCE')),
    'real-stock private proof requires explicit HANDOFF_* paths')
class RealStock(unittest.TestCase):
    def test_old_to_new_and_missing_observer_recovery(self):
        from fixtures.background_provider import ResponsesProvider
        from test_background_integration import wait, stop, stop_observers
        from codex_plus.rpc import Client

        old = Path(os.environ['HANDOFF_OLD_PACKAGE']).resolve()
        new = Path(os.environ['HANDOFF_NEW_PACKAGE']).resolve()
        binary = Path(os.environ['HANDOFF_OFFICIAL_DAEMON']).resolve()
        evidence = Path(os.environ['HANDOFF_EVIDENCE']).resolve()
        evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        summaries = []
        for mode in ('old-to-new', 'missing-running', 'missing-completed'):
            missing = mode != 'old-to-new'
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='hs-') as tmp:
                root = Path(tmp)
                home, workspace = root / 'home', root / 'workspace'
                home.mkdir(mode=0o700)
                workspace.mkdir()
                release = workspace / 'release'
                provider = ResponsesProvider(f'while [ ! -e {release} ]; do sleep 0.1; done; echo complete')
                env = {key: value for key, value in os.environ.items()
                       if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
                env.update(CODEX_HOME=str(home), PYTHONPATH=str(old), PYTHONDONTWRITEBYTECODE='1')
                (home / 'config.toml').write_text(f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
analytics.enabled=false
features.daemon_auto_start=false
features.unified_exec=true
features.shell_tool=true
features.plugins=false
[model_providers.isolated]
name="loopback handoff"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
''')
                case = evidence / mode
                case.mkdir(mode=0o700, exist_ok=True)
                native = rpc = trial = contender = None
                try:
                    with (case / 'native.log').open('w') as log:
                        native = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'],
                            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                    wait(lambda: (home / 'app-server-control/app-server-control.sock').exists(), 'stock daemon startup')
                    rpc = Client(home / 'app-server-control/app-server-control.sock')
                    self.assertIsNot(rpc.initialize_result.get('queueIdempotency'), True)
                    parent = rpc.call('thread/start', {'model': 'gpt-6.1-sol', 'cwd': str(workspace),
                        'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})['thread']['id']
                    rpc.call('turn/start', {'threadId': parent,
                        'input': [{'type': 'text', 'text': 'Run held command.', 'text_elements': []}], 'effort': 'high'})
                    wait(lambda: provider.count == 2, 'command launch')
                    process = rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data'][0]['processId']
                    # Exercise old-schema migration using the actual old packaged Store.
                    registered = subprocess.run([sys.executable, '-m', 'codex_plus.background', 'park',
                        '--parent', parent, '--process', process, '--reason', 'private handoff acceptance'],
                        cwd=old, env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(registered.returncode, 0, registered.stderr)
                    wait(lambda: len(handoff.helpers(parent, home)) == 2, 'old helpers startup')
                    if mode == 'old-to-new':
                        contender = subprocess.Popen([sys.executable, '-m', 'codex_plus.background', 'supervise',
                            '--parent', parent], cwd=old,
                            env=dict(env, PYTHONPATH=os.pathsep.join((str(old), str(old)))),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
                        wait(lambda: len(handoff.helpers(parent, home)) == 3, 'old supervisor contender startup')
                    provider.final_gate.set()
                    wait(lambda: rpc.call('thread/read', {'threadId': parent, 'includeTurns': False})['thread']['status']['type'] == 'idle', 'root idle')
                    if missing:
                        for action in ('supervise', 'observe'):
                            for identity in handoff.helpers(parent, home):
                                if identity['action'] == action:
                                    handoff.stop_helper(identity, parent, home)
                        self.assertEqual(handoff.helpers(parent, home), [])
                    if mode == 'missing-completed':
                        release.touch()
                        wait(lambda: not rpc.call('thread/backgroundTerminals/list', {'threadId': parent})['data'], 'completed while observer absent')
                    # Same home, different parent: the trial is outside the signal target.
                    trial_parent = str(uuid.uuid4())
                    with sqlite3.connect(home / 'codex-plus/background.sqlite') as database:
                        database.execute('INSERT INTO parks (id,parent,origin,reason,state,deadline) VALUES (?,?,?,?,?,?)',
                            ('trial-park', trial_parent, 'trial-origin', 'private trial', 'armed', time.time()+1000))
                        database.execute('INSERT INTO jobs (id,park,kind,thread,item,process,status) VALUES (?,?,?,?,?,?,?)',
                            ('trial-job', 'trial-park', 'process', trial_parent, 'trial-item', 'trial-process', 'armed'))
                    trial = subprocess.Popen([sys.executable, '-m', 'codex_plus.background', 'observe', '--parent', trial_parent],
                        cwd=old, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    # A durable cancelled row and ambiguous frozen payload are never rearmed/reformatted.
                    cancelled_parent = parent
                    with sqlite3.connect(home / 'codex-plus/background.sqlite') as database:
                        database.execute('INSERT INTO parks (id,parent,origin,reason,state,deadline) VALUES (?,?,?,?,?,?)',
                            ('cancel-park', cancelled_parent, 'cancel-origin', 'private cancelled', 'cancelled', time.time()+1000))
                        database.execute('INSERT INTO jobs (id,park,kind,thread,item,process,status) VALUES (?,?,?,?,?,?,?)',
                            ('cancel-job', 'cancel-park', 'process', cancelled_parent, 'cancel-item', 'cancel-process', 'cancelled'))
                        database.execute('INSERT INTO callbacks (id,parent,client,state,message,idempotent) VALUES (?,?,?,?,?,?)',
                            ('frozen', parent, 'frozen-client', 'uncertain', 'original frozen ambiguous payload', 0))
                    before = handoff.snapshot(home, parent)
                    run = subprocess.run([sys.executable, str(REPO / 'scripts/handoff-observer.py'), '--handoff',
                        '--parent', parent, '--codex-home', str(home), '--package', str(new),
                        '--evidence-dir', str(case / 'audit')], capture_output=True, text=True, timeout=30)
                    (case / 'controller.stdout').write_text(run.stdout)
                    (case / 'controller.stderr').write_text(run.stderr)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    result = json.loads(run.stdout)
                    self.assertEqual(result['phase'], 'verified')
                    if not result['clean_complete_exit']:
                        observer = next(row for row in result['new_helpers'] if row['action'] == 'observe')
                        self.assertEqual(result['new_observer_lock_owner'], observer['pid'])
                    if mode == 'old-to-new':
                        actions = [row['action'] for row in result['stopped']]
                        self.assertEqual(actions.count('supervise'), 2)
                        self.assertNotIn('supervise', actions[actions.index('observe'):])
                    self.assertTrue(all(row['cwd'] == str(new) for row in result['new_helpers']))
                    with sqlite3.connect(case / 'audit/ledger.sqlite') as backup:
                        self.assertEqual(backup.execute("SELECT id FROM jobs WHERE id='trial-job'").fetchone()[0], 'trial-job')
                    self.assertIsNone(trial.poll(), 'unrelated private trial was killed')
                    if mode != 'missing-completed':
                        self.assertEqual(provider.count, 2, 'controller/monitoring invoked model')
                    handoff.retained(before, handoff.snapshot(home, parent))
                    release.touch()
                    wait(lambda: provider.count == 3, 'completion callback', timeout=20)
                    wait(lambda: any(row['state'] == 'delivered' for row in handoff.snapshot(home, parent)['callbacks']), 'delivery')
                    after = handoff.snapshot(home, parent)
                    handoff.retained(before, after)
                    delivered = [row for row in after['callbacks'] if row['state'] == 'delivered']
                    self.assertEqual(len(delivered), 1)
                    self.assertIn('REPORT ONLY' if mode == 'missing-completed' else 'LIVE COMPLETION', delivered[0]['message'])
                    frozen = next(row for row in after['callbacks'] if row['id'] == 'frozen')
                    self.assertEqual(frozen['message'], 'original frozen ambiguous payload')
                    self.assertEqual(frozen['state'], 'uncertain')
                    self.assertIsNone(frozen['queue'])
                    self.assertEqual(provider.count, 3, 'ambiguous frozen callback was resent')
                    summaries.append({'mode': mode, 'passed': True, 'parent': parent,
                        'provider_requests_during_handoff': 1 if mode == 'missing-completed' else 0,
                        'callback_kind': 'REPORT ONLY' if mode == 'missing-completed' else 'LIVE COMPLETION', 'completion_callbacks': 1,
                        'old_package': str(old), 'new_package': str(new), 'official_daemon': str(binary)})
                finally:
                    provider.final_gate.set()
                    release.touch()
                    stop_observers(home)
                    if trial:
                        stop(trial)
                    if contender:
                        stop(contender)
                    if rpc:
                        rpc.close()
                    if native:
                        stop(native)
                    provider.close()
        (evidence / 'summary.json').write_text(json.dumps(summaries, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
