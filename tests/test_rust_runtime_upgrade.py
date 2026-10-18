"""Cooperative upgrade gates using copied real Rust executables, never code hooks.

Run: python3 tests/test_rust_runtime_upgrade.py --runtime PATH --evidence DIRECTORY -v
"""

import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest

import test_rust_runtime_acceptance as acceptance
from test_rust_runtime_acceptance import CHILD, PARENT, NativeFake, Observer, Store, register, wait

RUNTIME = acceptance.RUNTIME
EVIDENCE = None


def seal(package, runtime, release, *, broken_exec=False, invalid_elf=False):
    target = package / 'bin/codex-plus-runtime'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(runtime, target)
    if broken_exec:
        data = bytearray(target.read_bytes())
        assert data[:6] == b'\x7fELF\x02\x01', 'fixture needs a native little-endian ELF64 runtime'
        phoff = struct.unpack_from('<Q', data, 32)[0]
        size, count = struct.unpack_from('<HH', data, 54)
        for index in range(count):
            offset = phoff + index * size
            if struct.unpack_from('<I', data, offset)[0] == 3:
                start = struct.unpack_from('<Q', data, offset + 8)[0]
                length = struct.unpack_from('<Q', data, offset + 32)[0]
                interpreter = b'/no-such-runtime-loader'
                assert len(interpreter) < length
                data[start:start + length] = interpreter.ljust(length, b'\0')
                break
        else:
            raise AssertionError('exec-failure gate needs a dynamic ELF interpreter')
        target.write_bytes(data)
    if invalid_elf:
        # Preserve the validated ELF identity while omitting the kernel's program headers.
        target.write_bytes(target.read_bytes()[:64])
    target.chmod(0o755)
    machine = struct.unpack_from('<H', target.read_bytes(), 18)[0]
    architecture = {62: 'x86_64', 183: 'aarch64'}[machine]
    files = {'bin/codex-plus-runtime': {
        'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'size': target.stat().st_size, 'mode': '0755'}}
    manifest = {'schema_version': 1, 'coordinator_protocol': 5, 'execution_record_format': 1,
                'release': f'0.163.0-plus.0.4.{release}', 'plus_commit': str(release) * 40,
                'upstream': {'version': '0.163.0', 'tag': 'private-stock-proof', 'commit': '0' * 40},
                'target': f'{architecture}-unknown-linux-gnu', 'files': files,
                'tree_sha256': hashlib.sha256(json.dumps(files, sort_keys=True,
                    separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()}
    (package / 'codex-plus.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
    return package


@unittest.skipUnless(RUNTIME, 'set CODEX_PLUS_TEST_RUNTIME or pass --runtime')
class RustUpgradeTests(unittest.TestCase):
    def setUp(self):
        acceptance.RUNTIME = RUNTIME
        acceptance.CURRENT_CASE = self.id().rsplit('.', 1)[-1]
        acceptance.EVIDENCE = EVIDENCE
        self.fixture = acceptance.PrivateRuntime(NativeFake())
        self.root = self.fixture.home / 'packages'
        self.packages = {name: seal(self.root / name, RUNTIME, index)
                         for index, name in enumerate('ABC')}
        self.flip('A')

    def tearDown(self):
        self.fixture.close()

    def flip(self, label):
        temporary = self.root / '.next'
        temporary.symlink_to(label)
        os.replace(temporary, self.root / 'current')

    def seed(self, frozen=False):
        f = self.fixture
        store = Store(f.home)
        try:
            if frozen:
                register(store, f.fake, PARENT, [CHILD], [], '0.2 frozen upgrade history')
                f.fake.complete()
                Observer(store, PARENT, lambda: f.fake).step()
                callback = store.claim_callback(store.status(PARENT)['callbacks'][0], native=True)
                store.dispatch_result(callback, acknowledged=False)
            f.fake.running = [{'processId': '7812', 'itemId': 'upgrade-command'}]
            register(store, f.fake, PARENT, [], ['7812'], 'registered upgrade shell')
            state = store.status(PARENT)
        finally:
            store.close()
        return state

    def spawn(self, label='A', *, action='observe', cwd=None):
        f = self.fixture
        log = (f.home / f'upgrade-{len(f.children)}.log').open('w+')
        process = subprocess.Popen([str(self.packages[label] / 'bin/codex-plus-runtime'),
                                    action, '--parent', PARENT],
                                   cwd=cwd or self.packages[label], env=f.env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        f.children.append((process, log))
        return process

    def test_relative_home_survives_packaged_roles_failed_exec_and_upgrade(self):
        f = self.fixture
        launch = f.home / 'launch space'
        launch.mkdir()
        (f.home / 'anchor space').mkdir()
        relative = '../anchor space/..'
        f.env['CODEX_HOME'] = relative
        self.seed()
        for action in ('observe', 'supervise'):
            with self.subTest(action=action):
                previous_owner = f.state()['jobs'][0]['observer_run']
                if action == 'supervise':
                    f.supervisor_lock.close()
                process = self.spawn(action=action, cwd=launch)
                wait(lambda: f.claimed() and f.state()['jobs'][0]['observer_run'] != previous_owner,
                     f'packaged {action} lost relative home at startup')
                wait(lambda: Path(f'/proc/{process.pid}/cwd').resolve() == self.packages['A'],
                     f'{action} did not retain package cwd')
                self.assertIn(f'CODEX_HOME={launch}/{relative}'.encode(),
                              Path(f'/proc/{process.pid}/environ').read_bytes().split(b'\0'))
                if action == 'observe':
                    process.terminate()
                    process.wait(timeout=5)
        seal(self.packages['B'], RUNTIME, 1, broken_exec=True)
        self.flip('B')
        wait(lambda: any(p['error'] == 'Coordinator upgrade unavailable; continuing installed observer.'
                         for p in f.state()['parks']), 'relative home lost after failed exec')
        self.assertTrue(self.executing(process, 'A'))
        self.flip('C')
        wait(lambda: self.executing(process, 'C'), 'relative home lost during cooperative upgrade')
        self.complete()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(f.fake.starts), 1)
        for package in self.packages.values():
            self.assertFalse((package / 'codex-plus').exists(), 'created package-local ledger')
            self.assertFalse((package / 'anchor space').exists(), 'resolved home against package cwd')

    def test_relative_home_keeps_state_symlink_rejection(self):
        f = self.fixture
        launch = f.home / 'launch space'
        launch.mkdir()
        (f.home / 'anchor space').mkdir()
        f.env['CODEX_HOME'] = '../anchor space/..'
        f.supervisor_lock.close()
        shutil.rmtree(f.home / 'codex-plus')
        outside = f.home / 'outside'
        outside.mkdir()
        (f.home / 'codex-plus').symlink_to(outside, target_is_directory=True)
        try:
            process = self.spawn(cwd=launch)
            process.wait(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse((outside / 'background.sqlite').exists())
        finally:
            (f.home / 'codex-plus').unlink()
            (f.home / 'codex-plus').mkdir()

    def executing(self, process, label):
        path = Path(f'/proc/{process.pid}/exe')
        try:
            return path.resolve(strict=True) == self.packages[label] / 'bin/codex-plus-runtime'
        except OSError:
            return False

    def complete(self):
        f = self.fixture
        f.fake.running.clear()
        f.fake.entries.append({'turnId': 'origin', 'item': {
            'type': 'commandExecution', 'id': 'upgrade-command', 'processId': '7812',
            'status': 'completed', 'exitCode': 0}})
        f.fake.idle()
        wait(f.delivered, 'upgraded runtime did not settle callbacks')

    def test_real_native_cooperative_reexec_preserves_frozen_ids_and_running_work(self):
        f = self.fixture
        before = self.seed(frozen=True)
        frozen = before['callbacks'][0]
        process = self.spawn()
        wait(f.claimed, 'old native package did not claim')
        old_owner = next(j['observer_run'] for j in f.state()['jobs'] if j['kind'] == 'process')
        self.flip('B')
        wait(lambda: self.executing(process, 'B'), 'native observer did not reexec sibling package')
        self.assertEqual(Path(f'/proc/{process.pid}/cwd').resolve(), self.packages['B'])
        wait(lambda: any(j['kind'] == 'process' and j['observer_run'] != old_owner
                         and not j['recovery_pending'] for j in f.state()['jobs']),
             'new package did not reconfirm the running job')
        self.complete()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        state = f.state()
        after = next(c for c in state['callbacks'] if c['id'] == frozen['id'])
        self.assertEqual({key: frozen[key] for key in ('id', 'client', 'message', 'idempotent')},
                         {key: after[key] for key in ('id', 'client', 'message', 'idempotent')})
        self.assertEqual({j['id'] for j in before['jobs']}, {j['id'] for j in state['jobs']})
        self.assertEqual(len(f.fake.starts), 2)
        shell = next(j for j in state['jobs'] if j['kind'] == 'process')
        self.assertEqual((shell['status'], shell['report']), ('completed', 0))

    def test_blocked_contender_reselects_rapid_current_before_ledger_claim(self):
        f = self.fixture
        before = self.seed()
        owner = (f.home / 'codex-plus' / f'{PARENT}.lock').open('w')
        fcntl.flock(owner, fcntl.LOCK_EX)
        process = self.spawn()
        try:
            self.flip('B')
            self.flip('C')
            self.assertFalse(f.claimed(), 'contender opened ledger before getting role lock')
        finally:
            owner.close()
        wait(lambda: self.executing(process, 'C'), 'blocked contender used stale current package')
        wait(f.claimed, 'latest package did not claim jobs')
        self.complete()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual({j['id'] for j in before['jobs']}, {j['id'] for j in f.state()['jobs']})
        self.assertEqual(len(f.fake.starts), 1)

    def test_valid_manifest_failed_exec_keeps_old_runtime_then_accepts_new_current(self):
        f = self.fixture
        self.seed()
        process = self.spawn()
        wait(f.claimed, 'old package did not claim')
        seal(self.packages['B'], RUNTIME, 1, broken_exec=True)
        self.flip('B')
        wait(lambda: f.state()['parks'][0]['error'] ==
             'Coordinator upgrade unavailable; continuing installed observer.',
             'exec failure did not publish bounded fallback warning')
        self.assertTrue(self.executing(process, 'A'))
        self.assertIsNone(process.poll())
        self.flip('C')
        wait(lambda: self.executing(process, 'C'), 'failed package permanently blocked later upgrade')
        self.complete()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(f.fake.starts), 1)

    def test_invalid_target_is_rejected_without_losing_registered_work(self):
        f = self.fixture
        self.seed()
        manifest_path = self.packages['B'] / 'codex-plus.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['target'] = 'wrong-target'
        manifest_path.write_text(json.dumps(manifest))
        self.flip('B')
        process = self.spawn()
        wait(f.claimed, 'installed runtime did not continue after rejecting candidate')
        self.assertTrue(self.executing(process, 'A'))
        self.assertEqual(f.state()['parks'][0]['error'],
                         'Coordinator upgrade unavailable; continuing installed observer.')
        self.complete()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(f.fake.starts), 1)

    def test_kernel_enoexec_keeps_owner_cwd_and_frozen_callback_then_upgrades_once(self):
        f = self.fixture
        before = self.seed(frozen=True)
        frozen = before['callbacks'][0]
        process = self.spawn()
        wait(f.claimed, 'old package did not claim')
        seal(self.packages['B'], RUNTIME, 1, invalid_elf=True)
        with self.assertRaises(OSError) as error:
            subprocess.Popen([str(self.packages['B'] / 'bin/codex-plus-runtime')])
        self.assertEqual(error.exception.errno, errno.ENOEXEC)
        self.flip('B')
        wait(lambda: any(p['error'] == 'Coordinator upgrade unavailable; continuing installed observer.'
                         for p in f.state()['parks']), 'kernel ENOEXEC did not leave a fallback warning')
        self.assertTrue(self.executing(process, 'A'))
        self.assertEqual(Path(f'/proc/{process.pid}/cwd').resolve(), self.packages['A'])
        self.assertIsNone(process.poll())
        after_failure = next(c for c in f.state()['callbacks'] if c['id'] == frozen['id'])
        self.assertEqual({k: frozen[k] for k in ('id', 'client', 'message', 'idempotent')},
                         {k: after_failure[k] for k in ('id', 'client', 'message', 'idempotent')})
        self.flip('C')
        wait(lambda: self.executing(process, 'C'), 'valid package did not recover after kernel ENOEXEC')
        self.complete()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(f.fake.starts), 2)
        self.assertEqual(len(f.fake.entries), 3)  # Two consumed callbacks and the one command completion.
        self.assertEqual(len(f.state()['callbacks']), 2)
        final = next(c for c in f.state()['callbacks'] if c['id'] == frozen['id'])
        self.assertEqual({k: frozen[k] for k in ('id', 'client', 'message', 'idempotent')},
                         {k: final[k] for k in ('id', 'client', 'message', 'idempotent')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--evidence', type=Path)
    args, remaining = parser.parse_known_args()
    RUNTIME = args.runtime.resolve(strict=True)
    EVIDENCE = args.evidence.resolve() if args.evidence else None
    RustUpgradeTests.__unittest_skip__ = False
    RustUpgradeTests.__unittest_skip_why__ = ''
    unittest.main(argv=[sys.argv[0], *remaining])
