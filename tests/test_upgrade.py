"""Cooperative package selection, lock ordering, and settled-cycle handoff."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from codex_plus import background, upgrade
from codex_plus.ledger import UPGRADE_WARNING, Store
from test_background import Fake, PARENT, CHILD


def seal(root, **changes):
    files = {p.relative_to(root).as_posix(): {
        'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
        'size': p.stat().st_size, 'mode': f'{p.stat().st_mode & 0o7777:04o}'}
        for p in root.rglob('*') if p.is_file() and p.name != 'codex-plus.json'}
    manifest = {'schema_version': 1, 'coordinator_protocol': 1, 'release': 'fixture-plus.0.1.1',
                'upstream': {'version': 'fixture', 'tag': 'fixture', 'commit': 'same'},
                'target': 'fixture-linux', 'files': files,
                'tree_sha256': hashlib.sha256(json.dumps(files, sort_keys=True,
                    separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()}
    manifest.update(changes)
    (root / 'codex-plus.json').write_text(json.dumps(manifest))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old, self.new = self.base / 'old', self.base / 'new'
        for root in (self.old, self.new):
            (root / 'codex_plus').mkdir(parents=True)
            for name in ('__init__', 'background', 'upgrade', 'ledger', 'rpc', 'evidence'):
                (root / f'codex_plus/{name}.py').write_text(f'# {name}\n')
            seal(root, release='fixture-plus.0.1.0' if root == self.old else 'fixture-plus.0.1.1')
        self.current = self.base / 'current'
        self.current.symlink_to('new')

    def test_compatible_and_readonly_packages_select_without_native_hashing(self):
        self.assertEqual(upgrade.select_package(self.old), self.new)
        for path in self.new.rglob('*.py'):
            path.chmod(path.stat().st_mode & ~0o222)
        self.assertEqual(upgrade.select_package(self.old), self.new)
        (self.new / 'bin').mkdir()
        (self.new / 'bin/native').write_text('not read by coordinator')
        self.assertEqual(upgrade.select_package(self.old), self.new)

    def test_development_no_current_and_current_self_disable_selection(self):
        self.current.unlink()
        self.assertIsNone(upgrade.select_package(self.old))
        self.current.symlink_to('old')
        self.assertIsNone(upgrade.select_package(self.old))
        (self.old / 'codex-plus.json').unlink()
        self.assertIsNone(upgrade.select_package(self.old))

    def test_different_folder_equal_or_lower_versions_are_refused(self):
        seal(self.old, release='fixture-plus.0.1.9')
        for release in ('fixture-plus.0.1.9', 'fixture-plus.0.1.8', 'fixture-plus.0.0.99'):
            with self.subTest(release=release):
                seal(self.new, release=release)
                with self.assertRaises(ValueError):
                    upgrade.select_package(self.old)

    def test_release_versions_compare_numerically_across_patch_minor_and_major(self):
        for previous, following in (('0.1.9', '0.1.10'), ('0.9.99', '0.10.0'),
                                    ('0.99.99', '1.0.0'), ('9.0.0', '10.0.0')):
            with self.subTest(previous=previous, following=following):
                seal(self.old, release=f'fixture-plus.{previous}')
                seal(self.new, release=f'fixture-plus.{following}')
                self.assertEqual(upgrade.select_package(self.old), self.new)

    def test_missing_or_noncanonical_release_on_either_package_is_refused(self):
        malformed = (None, '', 1, 'other-plus.0.1.2', 'fixture-plus.1.2',
                     'fixture-plus.1.2.3.4', 'fixture-plus.01.2.3', 'fixture-plus.1.02.3',
                     'fixture-plus.1.2.03', 'fixture-plus.-1.2.3', 'fixture-plus.1.2.3-rc.1',
                     'fixture-plus.1.2.3+build', 'fixture-plus.1.2.3\n')
        for root in (self.old, self.new):
            for release in malformed:
                with self.subTest(root=root.name, release=release):
                    seal(self.old, release='fixture-plus.0.1.0')
                    seal(self.new, release='fixture-plus.0.1.1')
                    seal(root, release=release)
                    if release is None:
                        path = root / 'codex-plus.json'
                        manifest = json.loads(path.read_text())
                        del manifest['release']
                        path.write_text(json.dumps(manifest))
                    with self.assertRaises(ValueError):
                        upgrade.select_package(self.old)
        seal(self.old, release='malformed')
        self.current.unlink()
        self.current.symlink_to('old')
        self.assertIsNone(upgrade.select_package(self.old))

    def test_incompatible_identity_protocol_and_inventory_are_rejected(self):
        for changes in ({'target': 'different'}, {'upstream': {'version': 'fixture'}},
                        {'coordinator_protocol': 2}, {'coordinator_protocol': True},
                        {'tree_sha256': '0' * 64}):
            with self.subTest(changes=changes):
                seal(self.new, **changes)
                with self.assertRaises(ValueError):
                    upgrade.select_package(self.old)
        seal(self.new)
        seal(self.old, coordinator_protocol=True)
        with self.assertRaises(ValueError):
            upgrade.select_package(self.old)

    def test_modified_extra_missing_and_symlink_helpers_are_rejected(self):
        path = self.new / 'codex_plus/rpc.py'
        path.write_text('changed')
        with self.assertRaises(ValueError):
            upgrade.select_package(self.old)
        seal(self.new)
        extra = self.new / 'codex_plus/extra.py'
        extra.write_text('extra')
        with self.assertRaises(ValueError):
            upgrade.select_package(self.old)
        extra.unlink()
        path.unlink()
        with self.assertRaises(ValueError):
            upgrade.select_package(self.old)
        path.symlink_to(self.old / 'codex_plus/rpc.py')
        with self.assertRaises(ValueError):
            upgrade.select_package(self.old)

    def test_current_must_point_directly_to_sibling_and_manifest_must_be_regular(self):
        self.current.unlink()
        (self.base / 'alias').symlink_to(self.new)
        self.current.symlink_to('alias')
        with self.assertRaises(ValueError):
            upgrade.select_package(self.old)
        self.current.unlink()
        self.current.symlink_to('new')
        path = self.new / 'codex-plus.json'
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            upgrade.select_package(self.old)

    def test_exec_uses_current_interpreter_clean_pythonpath_and_restores_cwd_on_failure(self):
        previous = Path.cwd()
        with patch('codex_plus.upgrade.os.execve', side_effect=OSError('cannot exec')) as execute:
            with self.assertRaises(OSError):
                upgrade.exec_package(self.new, 'observe', PARENT, self.base)
        self.assertEqual(Path.cwd(), previous)
        executable, argv, env = execute.call_args.args
        self.assertEqual(executable, background.sys.executable)
        self.assertEqual(argv[1:4], ['-m', 'codex_plus.background', 'observe'])
        self.assertEqual(env['PYTHONPATH'], str(self.new))
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')

    def test_exec_window_keeps_old_package_in_actual_launch_argv_until_exec_succeeds(self):
        with patch('codex_plus.background.PACKAGE_ROOT', self.old), \
                patch('codex_plus.background.subprocess.Popen') as popen:
            background.launch(PARENT, self.base)
        launched = popen.call_args.args[0]
        previous = Path.cwd()
        os.chdir(self.old)
        def execute(_executable, argv, _env):
            self.assertEqual(Path.cwd(), self.new)
            # install.py identifies package paths in /proc/cmdline with this trailing slash.
            self.assertIn(str(self.old) + '/', ' '.join(background.sys.argv))
            self.assertIn(str(self.new) + '/', argv)
            self.assertNotIn(str(self.old) + '/', argv)
            raise OSError('exec fails while installer inspects process')
        try:
            with patch.object(background.sys, 'argv', launched), \
                    patch('codex_plus.upgrade.os.execve', execute):
                with self.assertRaises(OSError):
                    upgrade.exec_package(self.new, 'supervise', PARENT, self.base,
                                         launched[3:])
            self.assertEqual(Path.cwd(), self.old)
        finally:
            os.chdir(previous)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Keep ordinary lifecycle fixtures independent of the imported package location.
        context = patch.multiple(background, PACKAGE_ROOT=Path(self.temp.name) / 'source',
                                 state_home=lambda: Path(self.temp.name))
        context.start()
        self.addCleanup(context.stop)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.close)
        self.fake = Fake()
        self.observer = background.Observer(self.store, PARENT, lambda: self.fake)
        background.register(self.store, self.fake, PARENT, [CHILD], [], 'upgrade fixture')
        self.target = Path(self.temp.name) / 'new'

    def test_switch_after_settled_queue_rpc_closes_client_and_preserves_frozen_message(self):
        self.fake.idle()
        self.fake.complete()
        self.fake.fail_add = True
        selected = []
        def select(_):
            selected.append(len(self.fake.calls))
            return self.target if self.fake.enqueues else None
        with patch('codex_plus.background.select_package', select), patch.object(self.fake, 'close') as close:
            self.assertEqual(self.observer.run(), upgrade.UPGRADE_EXIT)
        callback = self.store.status(PARENT)['callbacks'][0]
        self.assertEqual(callback['state'], 'uncertain')
        self.assertTrue(callback['message'])
        self.assertEqual(self.fake.enqueues, 1)
        close.assert_called()
        self.assertGreater(selected[-1], 0)
        snapshot = {k: callback[k] for k in ('id', 'client', 'message')}
        self.store.request_cancel(PARENT)
        replacement = background.Observer(self.store, PARENT, lambda: self.fake)
        replacement.recover()
        replacement.step()
        after = self.store.status(PARENT)['callbacks'][0]
        self.assertEqual({k: after[k] for k in snapshot}, snapshot)
        self.assertEqual(self.fake.enqueues, 1)
        self.assertNotEqual(replacement.run_id, self.observer.run_id)

    def test_after_start_upgrade_waits_for_delivered_dispatch_and_keeps_frozen_identity(self):
        self.fake.idle()
        self.fake.complete()
        advertised, frozen, selection_states = [], [], []
        def fault(point):
            if point == 'after_start':
                callback = self.store.status(PARENT)['callbacks'][0]
                self.assertEqual(callback['state'], 'starting')
                self.assertEqual(self.fake.starts, [callback['queue']])
                frozen.append({key: callback[key] for key in ('id', 'client', 'message', 'queue')})
                advertised.append(True)
        def select(_):
            if advertised:
                callback = self.store.status(PARENT)['callbacks'][0]
                selection_states.append(callback['state'])
                self.assertEqual(callback['state'], 'delivered')
                return self.target
        self.observer.fault = fault
        with patch('codex_plus.background.select_package', select), patch.object(self.fake, 'close') as close:
            self.assertEqual(self.observer.run(), 75)
        close.assert_called_once()
        self.assertEqual(selection_states, ['delivered'])
        replacement = background.Observer(self.store, PARENT, lambda: self.fake)
        replacement.recover()
        self.assertFalse(replacement.step())
        callback = self.store.status(PARENT)['callbacks'][0]
        self.assertEqual({key: callback[key] for key in frozen[0]}, frozen[0])
        self.assertEqual(callback['state'], 'delivered')
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len(self.fake.starts), 1)
        messages = [entry['item'] for entry in self.fake.entries if entry['item']['type'] == 'userMessage']
        self.assertEqual([item['clientId'] for item in messages], [callback['client']])

    def test_running_worker_handoff_reconfirms_exact_turn_then_delivers_live_once(self):
        self.store.request_cancel(PARENT)
        cancelled = self.store.status(PARENT)
        cancelled_park, cancelled_job = cancelled['parks'][0], cancelled['jobs'][0]
        self.fake.parent_turns = [{'id': 'origin-next', 'status': 'inProgress'}]
        self.fake.child_turns = [{'id': 'work-next', 'status': 'inProgress'}]
        self.fake.child_runtime_status = 'active'
        background.register(self.store, self.fake, PARENT, [CHILD], [], 'survive upgrade')
        self.assertTrue(self.observer.step())
        job = next(row for row in self.store.status(PARENT)['jobs'] if row['status'] == 'armed')
        identity = {key: job[key] for key in ('id', 'park', 'thread', 'turn')}
        with patch('codex_plus.background.select_package', side_effect=[None, self.target]):
            self.assertEqual(self.observer.run(), 75)
        replacement = background.Observer(self.store, PARENT, lambda: self.fake)
        replacement.recover()
        recovering = next(row for row in self.store.status(PARENT)['jobs'] if row['id'] == job['id'])
        self.assertEqual(recovering['recovery_pending'], 1)
        self.assertEqual(recovering['observer_run'], replacement.run_id)
        self.assertNotEqual(replacement.run_id, self.observer.run_id)
        self.fake.calls.clear()
        self.assertTrue(replacement.step())
        reconfirmed = next(row for row in self.store.status(PARENT)['jobs'] if row['id'] == job['id'])
        self.assertEqual((reconfirmed['status'], reconfirmed['recovery_pending']), ('armed', 0))
        self.assertEqual({key: reconfirmed[key] for key in identity}, identity)
        self.assertTrue(any(method == 'thread/turns/list' and params['threadId'] == CHILD
                            for method, params in self.fake.calls))
        self.fake.complete()
        self.fake.idle()
        self.assertFalse(replacement.step())
        self.assertFalse(replacement.step())
        final = self.store.status(PARENT)
        callback = final['callbacks'][0]
        self.assertIn('LIVE COMPLETION', callback['message'])
        self.assertEqual(callback['state'], 'delivered')
        self.assertEqual(self.fake.enqueues, 1)
        self.assertEqual(len(self.fake.starts), 1)
        completed = next(row for row in final['jobs'] if row['id'] == job['id'])
        self.assertEqual({key: completed[key] for key in identity}, identity)
        self.assertEqual((completed['status'], completed['report']), ('completed', 0))
        self.assertEqual(next(row for row in final['parks'] if row['id'] == cancelled_park['id']), cancelled_park)
        self.assertEqual(next(row for row in final['jobs'] if row['id'] == cancelled_job['id']), cancelled_job)

    def test_invalid_target_keeps_observing_with_durable_warning(self):
        states, published = [], []
        original_publish = self.store.publish
        def publish(parent):
            view = original_publish(parent)
            if self.store.active_parks(parent):
                published.append(view['state'])
            return view
        def sleep(_):
            with StoreContext(self.temp.name) as other:
                states.append(other.status(PARENT)['health']['state'])
                self.assertEqual(other.status(PARENT)['parks'][0]['error'], UPGRADE_WARNING)
            self.store.request_cancel(PARENT)
        with patch('codex_plus.background.select_package', side_effect=ValueError('bad candidate')), \
                patch('codex_plus.background.time.sleep', sleep), \
                patch.object(self.store, 'publish', side_effect=publish):
            self.assertIsNone(self.observer.run())
        self.assertEqual(states, ['error'])
        self.assertTrue(published)
        self.assertEqual(set(published), {'error'})

    def test_upgrade_warning_clears_on_valid_selection_or_completed_registration(self):
        self.store.upgrade_warning(PARENT, True)
        with patch('codex_plus.background.select_package', return_value=None):
            self.assertIsNone(background.check_upgrade(self.store, PARENT))
        self.assertEqual(self.store.publish(PARENT)['state'], 'waiting')
        self.store.upgrade_warning(PARENT, True)
        self.fake.complete()
        self.fake.idle()
        self.assertFalse(self.observer.step())
        self.store.monitoring_succeeded(PARENT)
        self.assertEqual(self.store.publish(PARENT)['state'], 'idle')

    def test_invalid_target_stays_visible_when_rpc_monitoring_recovers(self):
        published, sleeps = [], []
        original_publish = self.store.publish
        self.fake.fail_read = True
        def publish(parent):
            view = original_publish(parent)
            if self.store.active_parks(parent):
                published.append(view['state'])
            return view
        def sleep(_):
            sleeps.append(True)
            self.fake.fail_read = False
            if len(sleeps) == 2:
                self.store.request_cancel(PARENT)
        with patch('codex_plus.background.select_package', side_effect=ValueError('bad candidate')), \
                patch('codex_plus.background.time.sleep', sleep), \
                patch.object(self.store, 'publish', side_effect=publish):
            self.assertIsNone(self.observer.run())
        self.assertTrue(published)
        self.assertEqual(set(published), {'error'})

    def test_lower_current_keeps_watching_and_preserves_frozen_callback_and_cancelled_history(self):
        self.fake.idle()
        self.fake.complete()
        self.assertFalse(self.observer.step())
        frozen = self.store.status(PARENT)['callbacks'][0]
        self.store.request_cancel(PARENT, 'origin')
        history = self.store.status(PARENT)
        cancelled_park, cancelled_job = history['parks'][0], history['jobs'][0]
        self.fake.parent_status = 'active'
        self.fake.parent_turns = [{'id': 'next-origin', 'status': 'inProgress'}]
        self.fake.child_turns = [{'id': 'next-work', 'status': 'inProgress'}]
        background.register(self.store, self.fake, PARENT, [CHILD], [], 'keep watching lower current')
        packages = Path(self.temp.name) / 'packages'
        own, lower = packages / 'old', packages / 'new'
        for root, version in ((own, '0.1.10'), (lower, '0.1.9')):
            (root / 'codex_plus').mkdir(parents=True)
            for name in ('__init__', 'background', 'upgrade', 'ledger', 'rpc', 'evidence'):
                (root / f'codex_plus/{name}.py').write_text(f'# {name}\n')
            seal(root, release=f'fixture-plus.{version}')
        (packages / 'current').symlink_to('new')
        warnings = []
        def sleep(_):
            with StoreContext(self.temp.name) as reader:
                view = reader.status(PARENT)
                warnings.append(view['health']['state'])
                self.assertEqual(next(p for p in view['parks'] if p['state'] == 'armed')['error'], UPGRADE_WARNING)
                self.assertEqual(view['callbacks'][0], frozen)
            self.fake.complete()
            self.fake.idle()
        with patch('codex_plus.background.PACKAGE_ROOT', own), \
                patch('codex_plus.background.time.sleep', sleep):
            self.assertIsNone(self.observer.run())
        final = self.store.status(PARENT)
        self.assertEqual(warnings, ['error'])
        self.assertEqual(next(p for p in final['parks'] if p['id'] == cancelled_park['id']), cancelled_park)
        self.assertEqual(next(j for j in final['jobs'] if j['id'] == cancelled_job['id']), cancelled_job)
        self.assertEqual(next(c for c in final['callbacks'] if c['id'] == frozen['id']), frozen)
        delivered = next(c for c in final['callbacks'] if c['id'] != frozen['id'])
        self.assertEqual(delivered['state'], 'delivered')
        self.assertIn('LIVE COMPLETION', delivered['message'])
        self.assertEqual(self.fake.enqueues, 2)

    def test_warning_preserves_existing_error_and_cancelled_history(self):
        self.store.monitoring_failed(PARENT, 'ConnectionError')
        before = self.store.status(PARENT)['parks'][0]['error']
        self.store.upgrade_warning(PARENT, True)
        self.assertEqual(self.store.status(PARENT)['parks'][0]['error'], before)
        self.store.request_cancel(PARENT)
        snapshot = self.store.status(PARENT)
        self.store.upgrade_warning(PARENT, True)
        self.assertEqual(self.store.status(PARENT), snapshot)

    def test_upgrade_exit_does_not_consume_crash_budget_and_reverted_current_restarts(self):
        with patch('codex_plus.background.select_package', return_value=None), \
                patch('codex_plus.background.subprocess.Popen') as popen, \
                patch('codex_plus.background.time.sleep') as sleep:
            popen.return_value.wait.side_effect = [75, -9, 79, 79, 79, 79, 79]
            background.supervise(self.store, PARENT, self.temp.name)
        self.assertEqual(popen.call_count, 7)
        self.assertEqual(sleep.call_count, 5)
        self.assertIn('Supervisor restart limit', self.store.status(PARENT)['parks'][0]['error'])

    def test_supervisor_upgrade_retains_consumed_failure_budget(self):
        with patch.dict(os.environ, {'CODEX_PLUS_SUPERVISOR_FAILURES': '2'}), \
                patch('codex_plus.background.select_package', side_effect=[None, self.target]), \
                patch('codex_plus.background.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 75
            self.assertEqual(background.supervise(self.store, PARENT, self.temp.name), 75)
        self.assertEqual(self.store.supervisor_failures, 2)

    def test_main_lock_then_reselection_before_store_for_old_contender(self):
        order = []
        @contextmanager
        def lock(*_):
            order.append('lock')
            yield
            order.append('unlock')
        def select(_):
            order.append('select')
            return self.target
        class ExecStop(Exception):
            pass
        with patch('codex_plus.background.role_lock', lock), \
                patch('codex_plus.background.select_package', select), \
                patch('codex_plus.background.Store') as store, \
                patch('codex_plus.background.exec_package', side_effect=ExecStop):
            with self.assertRaises(ExecStop):
                background.main(['supervise', '--parent', PARENT])
        self.assertEqual(order, ['lock', 'select', 'unlock'])
        store.assert_not_called()

    def test_supervised_startup_returns_upgrade_without_opening_store(self):
        with patch.dict(os.environ, {'CODEX_PLUS_SUPERVISED': '1'}), \
                patch('codex_plus.background.state_home', return_value=Path(self.temp.name)), \
                patch('codex_plus.background.select_package', return_value=self.target), \
                patch('codex_plus.background.Store') as store, \
                patch('codex_plus.background.exec_package') as execute:
            self.assertEqual(background.main(['observe', '--parent', PARENT]), 75)
        store.assert_not_called()
        execute.assert_not_called()

    def test_old_explicit_park_redirects_before_store_and_preserves_selectors(self):
        argv = ['park', '--parent', PARENT, '--worker', CHILD, '--reason', 'keep selectors']
        class ExecStop(Exception):
            pass
        with patch('codex_plus.background.select_package', return_value=self.target), \
                patch('codex_plus.background.Store') as store, \
                patch('codex_plus.background.exec_package', side_effect=ExecStop) as execute:
            with self.assertRaises(ExecStop):
                background.main(argv)
        store.assert_not_called()
        self.assertEqual(execute.call_args.args[-1], argv)

    def test_standalone_cycle_closes_store_before_self_exec(self):
        opened = []
        def make_store(home):
            store = Store(home)
            opened.append(store)
            return store
        class ExecStop(Exception):
            pass
        def execute(*args):
            with self.assertRaises(background.sqlite3.ProgrammingError):
                opened[0]._db.execute('SELECT 1')
            self.assertEqual(args[1], 'observe')
            raise ExecStop()
        with patch.dict(os.environ, {'CODEX_PLUS_SUPERVISED': ''}), \
                patch('codex_plus.background.state_home', return_value=Path(self.temp.name)), \
                patch('codex_plus.background.select_package', side_effect=[None, self.target]), \
                patch('codex_plus.background.Store', make_store), \
                patch.object(background.Observer, 'run', return_value=75), \
                patch('codex_plus.background.exec_package', execute):
            with self.assertRaises(ExecStop):
                background.main(['observe', '--parent', PARENT])

    def test_packaged_direct_observer_bootstraps_own_argv_pin_before_store(self):
        root = Path(self.temp.name) / 'installed'
        root.mkdir()
        (root / 'codex-plus.json').write_text('{}')
        previous = Path.cwd()
        class ExecStop(Exception):
            pass
        def execute(package, action, parent, home, argv):
            self.assertEqual((package, action, Path.cwd()), (root, 'observe', root))
            self.assertEqual(argv, ['observe', '--parent', PARENT])
            self.assertEqual(upgrade.package_command(package, action, parent, argv)[-2:],
                             ['--package-root', str(root) + '/'])
            raise ExecStop()
        try:
            with patch('codex_plus.background.PACKAGE_ROOT', root), \
                    patch('codex_plus.background.state_home', return_value=Path(self.temp.name)), \
                    patch('codex_plus.background.Store') as store, \
                    patch('codex_plus.background.exec_package', execute):
                with self.assertRaises(ExecStop):
                    background.main(['observe', '--parent', PARENT])
                store.assert_not_called()
        finally:
            os.chdir(previous)

    def test_observer_child_command_contains_own_pin_and_wrong_pin_is_rejected(self):
        with patch('codex_plus.background.select_package', return_value=None), \
                patch('codex_plus.background.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 0
            background.supervise(self.store, PARENT, self.temp.name)
        self.assertEqual(popen.call_args.args[0][-2:], ['--package-root', str(background.PACKAGE_ROOT) + '/'])
        with patch('codex_plus.background.Store') as store, \
                patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                background.main(['observe', '--parent', PARENT, '--package-root', '/wrong/'])
        store.assert_not_called()

    def test_exec_failure_resumes_old_observer_once_with_warning_and_closed_store(self):
        stores, observed = [], []
        def make_store(home):
            store = Store(home)
            stores.append(store)
            return store
        def run(observer, lock_held=False):
            observed.append(observer.store.status(PARENT)['parks'][0]['error'])
            self.assertTrue(lock_held)
            return None
        with patch.dict(os.environ, {}, clear=False), \
                patch('codex_plus.background.FAILED_PACKAGE', None), \
                patch('codex_plus.background.state_home', return_value=Path(self.temp.name)), \
                patch('codex_plus.background.select_package', return_value=self.target), \
                patch('codex_plus.background.exec_package', side_effect=OSError('failure')) as execute, \
                patch('codex_plus.background.Store', make_store), \
                patch.object(background.Observer, 'run', run):
            self.assertEqual(background.main(['observe', '--parent', PARENT]), 0)
        self.assertEqual(observed, [UPGRADE_WARNING])
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(stores), 1)
        with self.assertRaises(background.sqlite3.ProgrammingError):
            stores[0]._db.execute('SELECT 1')


@contextmanager
def StoreContext(home):
    store = Store(home)
    try:
        yield store
    finally:
        store.close()


if __name__ == '__main__':
    unittest.main()
