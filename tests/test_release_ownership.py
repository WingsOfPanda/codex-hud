import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import sys
import subprocess
import select
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import release_protocol as protocol
spec = importlib.util.spec_from_file_location('ownership', ROOT/'scripts/release_ownership.py')
ownership = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ownership)


class Ownership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root/'proc'
        self.proc.mkdir()
        self.scope = self.root/'attempt'
        self.scope.mkdir()
        self.baseline = dict(boot_id='boot', processes={})

    def process(self, pid, *, cwd=None, env=b'', command=b'worker\0', ppid=1, ticks='123'):
        p = self.proc/str(pid)
        p.mkdir()
        fields = ['S', str(ppid)]+['0']*17+[ticks]+['0']*8
        (p/'stat').write_text(f'{pid} (worker) '+ ' '.join(fields))
        (p/'cmdline').write_bytes(command)
        (p/'environ').write_bytes(env)
        (p/'cwd').symlink_to(cwd or self.root)
        return p

    def audit(self, **kw):
        return ownership.inspect_processes([self.scope], self.baseline, scope=self.scope,
                                           proc_root=self.proc, current_boot='boot', **kw)

    def journal(self, *, code=0):
        identity = dict(boot_id='boot', pid=888881, start_ticks='12')
        return [dict(event='started', identity=identity, argv=['/pinned/helper'], exit_code=None),
                dict(event='finished', identity=identity, argv=['/pinned/helper'], exit_code=code)]

    def test_guard_journal_requires_exact_terminal_pair(self):
        rows = self.journal()
        self.assertTrue(ownership.verify_guard_journal(rows, 'boot'))
        for bad in ([], rows[:1], rows[::-1], rows+rows, self.journal(code=-9),
                    self.journal(code=1), self.journal(code=255)):
            self.assertFalse(ownership.verify_guard_journal(bad, 'boot'))
        self.assertFalse(ownership.verify_guard_journal(rows, 'another-boot'))
        changed = self.journal()
        changed[1] = dict(changed[1], argv=['different'])
        self.assertFalse(ownership.verify_guard_journal(changed, 'boot'))
        changed = self.journal()
        changed[1] = dict(changed[1], cmdline_sha256='different')
        self.assertFalse(ownership.verify_guard_journal(changed, 'boot'))

    def test_unpaired_exited_guard_still_blocks(self):
        report = self.audit(recorded=self.journal()[:1])
        self.assertFalse(report['passed'])
        self.assertEqual(report['unknown_unreadable_same_user_processes'],
                         [dict(reason='incomplete_guard_journal')])

    def test_failed_supervisor_cannot_downgrade_unreadable_process(self):
        proc = self.process(888885)
        original = Path.read_bytes
        def blocked(path):
            if path == proc/'environ':
                raise PermissionError('protected host process')
            return original(path)
        with patch.object(Path, 'read_bytes', blocked):
            rows = self.journal(code=1)
            with self.assertRaisesRegex(ValueError, 'exact terminal journal'):
                self.audit(recorded=rows, completed_guards=True)
            report = self.audit(recorded=rows)
            self.assertFalse(report['passed'])
            self.assertEqual(report['unattributed_host_processes'], [])

    def recovery_fixture(self):
        import copy
        execution = self.root/'job'
        execution.mkdir()
        package = self.root/'package'
        (package/'bin').mkdir(parents=True)
        (package/'bin/codex-plus-runtime').write_text('pinned executor')
        metadata = dict(attempt='attempt', socket_identity={}, socket_root=str(self.root/'socket'))
        profile = dict(executor_package_root=str(package), python=dict(path='/usr/bin/python3'),
                       tooling_root=str(ROOT))
        plan = dict(release='test')
        rows = self.journal()[:1]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        rows[0]['identity']['boot_id'] = boot
        (self.scope/'process-journal.jsonl').write_text(json.dumps(rows[0])+'\n')
        protocol.write_once(self.scope/'ownership-baseline.json', dict(boot_id=boot, processes={}))
        protocol.write_once(self.scope/'native-preparation.json', dict(fixture=True))
        binding = dict(attempt='attempt', attempt_root=str(self.scope), plan_sha256=protocol.digest(plan),
            journal_sha256=protocol.file_record(self.scope/'process-journal.jsonl')['sha256'],
            ownership_baseline_sha256=protocol.file_record(self.scope/'ownership-baseline.json')['sha256'],
            socket_identity={}, subtree_cleanup_evidence='Reviewed exact descendants and private fixture closure')
        resolution = dict(id='job', root='root', cleanup_verified=True,
                          evidence='Independently reviewed cleanup', release_recovery=binding)
        result = dict(id='job', root='root', state='failed', exit_code=1,
                      cleanup=dict(status='failed', exit_code=1))
        state = dict(id='job', root='root', state='failed', resolution=copy.deepcopy(resolution),
            result=copy.deepcopy(result), package=str(package),
            runtime=dict(sha256=protocol.file_record(package/'bin/codex-plus-runtime')['sha256']),
            manifest=dict(command=dict(spec=dict(argv=['/usr/bin/python3','-B',
                str(ROOT/'scripts/release_native.py'),'run','--attempt',str(self.scope),
                '--execution',str(execution)]))))
        for name, value in [('state',state), ('result',result), ('resolution',resolution)]:
            (execution/(name+'.json')).write_text(json.dumps(value))
        return execution, metadata, plan, profile, rows, state, result, resolution

    def test_recovery_requires_exact_explicit_resolution(self):
        execution, metadata, plan, profile, rows, state, result, resolution = self.recovery_fixture()
        binding = resolution['release_recovery']
        args = (self.scope,execution,metadata,plan,profile,state,result,resolution,
                binding['journal_sha256'],binding['ownership_baseline_sha256'])
        self.assertEqual(ownership.validate_resolution(*args)['attempt'], 'attempt')
        import copy
        for key, value in [('id','other'), ('cleanup_verified',False), ('evidence','')]:
            bad = copy.deepcopy(resolution)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                ownership.validate_resolution(*args[:7],bad,*args[8:])
        with self.assertRaisesRegex(ValueError,'binding mismatch'):
            ownership.validate_resolution(*args[:8],'changed',args[9])

    def test_recovery_preserves_failed_records_and_checks_seals(self):
        execution, metadata, plan, profile, rows, state, result, resolution = self.recovery_fixture()
        paths = [self.scope/'process-journal.jsonl', *execution.glob('*.json')]
        before = {p:p.read_bytes() for p in paths}
        native = types.SimpleNamespace(verify=lambda root: {})
        clean = dict(passed=True, live_owned_processes=[], unknown_unreadable_same_user_processes=[])
        with patch.dict(sys.modules,release_native=native), \
             patch.object(protocol,'read_attempt',return_value=(metadata,plan,profile)), \
             patch.object(ownership,'inspect_processes',return_value=clean) as audit:
            receipt = ownership.recover_evidence(self.scope,execution,rows)
            self.assertTrue(protocol.load(receipt)['qualification_unchanged'])
            receipt_bytes = receipt.read_bytes()
            self.assertEqual(ownership.recover_evidence(self.scope,execution,rows),receipt)
            self.assertEqual(receipt.read_bytes(),receipt_bytes)
            self.assertNotIn('completed_guards',audit.call_args.kwargs)
            self.assertNotIn('event',audit.call_args.kwargs['recorded'][0])
        self.assertEqual(before,{p:p.read_bytes() for p in paths})
        self.assertEqual(protocol.load(self.scope/'recovery/managed-result.json')['exit_code'],1)
        native.verify = lambda root: (_ for _ in ()).throw(ValueError('source seal changed'))
        with patch.dict(sys.modules,release_native=native), self.assertRaisesRegex(ValueError,'source seal'):
            ownership.recover_evidence(self.scope,execution,rows)

    def test_recovery_does_not_relax_live_or_unknown_processes(self):
        execution, metadata, plan, profile, rows, state, result, resolution = self.recovery_fixture()
        native = types.SimpleNamespace(verify=lambda root: {})
        with patch.dict(sys.modules,release_native=native), \
             patch.object(protocol,'read_attempt',return_value=(metadata,plan,profile)), \
             patch.object(ownership,'inspect_processes',return_value=dict(passed=False)):
            with self.assertRaisesRegex(ValueError,'ownership remains unresolved'):
                ownership.recover_evidence(self.scope,execution,rows)
        self.assertFalse((self.scope/'recovery/receipt.json').exists())

    def test_resolved_unknown_without_result_preserves_absence(self):
        execution, metadata, plan, profile, rows, state, result, resolution = self.recovery_fixture()
        state.update(state='unknown',result=None)
        (execution/'state.json').write_text(json.dumps(state))
        (execution/'result.json').unlink()
        native = types.SimpleNamespace(verify=lambda root: {})
        with patch.dict(sys.modules,release_native=native), \
             patch.object(protocol,'read_attempt',return_value=(metadata,plan,profile)), \
             patch.object(ownership,'inspect_processes',return_value=dict(passed=True)):
            receipt = ownership.recover_evidence(self.scope,execution,rows)
        self.assertTrue(protocol.load(receipt)['result_absent'])
        self.assertIsNone(protocol.load(self.scope/'recovery/managed-result.json'))
        self.assertFalse((execution/'result.json').exists())

    def test_deleted_cwd_remains_owned(self):
        owned = self.scope/'deleted'
        owned.mkdir()
        proc = self.process(888882, cwd=str(owned)+' (deleted)')
        owned.rmdir()
        report = self.audit()
        self.assertFalse(report['passed'])
        self.assertEqual(report['live_owned_processes'][0]['pid'], int(proc.name))

    def test_control_ancestor_exclusion_only_affects_argv(self):
        self.process(os.getpid(), ppid=888883)
        parent = self.process(888883, command=os.fsencode(self.scope/'controller.py')+b'\0')
        with patch.object(ownership.os, 'getppid', return_value=888883):
            self.assertTrue(self.audit()['passed'])
            (parent/'environ').write_bytes(f'CODEX_PLUS_RELEASE_QA_SCOPE={self.scope}'.encode())
            self.assertFalse(self.audit()['passed'])
            (parent/'environ').write_bytes(b'')
            (parent/'cwd').unlink()
            (parent/'cwd').symlink_to(self.scope)
            self.assertFalse(self.audit()['passed'])

    def test_identical_argv_nonancestor_blocks(self):
        self.process(888884, command=os.fsencode(self.scope/'controller.py')+b'\0')
        self.assertFalse(self.audit()['passed'])

    def test_recorded_owner_wins_over_terminal_journal(self):
        self.process(888881, ticks='12')
        self.assertFalse(self.audit(recorded=self.journal(), completed_guards=True)['passed'])

    def test_unknown_host_process_needs_completed_local_supervision(self):
        proc = self.process(888885)
        original = Path.read_bytes
        def blocked(path):
            if path == proc/'environ':
                raise PermissionError('protected unrelated host process')
            return original(path)
        with patch.object(Path, 'read_bytes', blocked):
            self.assertFalse(self.audit()['passed'])
            report = self.audit(recorded=self.journal(), completed_guards=True)
            self.assertTrue(report['passed'])
            self.assertEqual(report['unattributed_host_processes'][0]['pid'], 888885)
            self.assertEqual(report['unknown_unreadable_same_user_processes'], [])
            # Even an unreadable environment cannot hide positive cwd ownership.
            (proc/'cwd').unlink()
            (proc/'cwd').symlink_to(self.scope)
            self.assertFalse(self.audit(recorded=self.journal(), completed_guards=True)['passed'])

    def test_completed_flag_cannot_replace_evidence(self):
        with self.assertRaisesRegex(ValueError, 'terminal journal'):
            self.audit(completed_guards=True)

    def test_preexisting_baseline_is_exact_and_cannot_mask_owner(self):
        proc = self.process(888886)
        self.baseline['processes'][proc.name] = ownership.process_identity(proc)
        original = Path.read_bytes
        def blocked(path):
            if path == proc/'environ':
                raise PermissionError('protected')
            return original(path)
        with patch.object(Path, 'read_bytes', blocked):
            self.assertTrue(self.audit()['passed'])
            (proc/'cmdline').write_bytes(os.fsencode(self.scope/'child')+b'\0')
            self.assertFalse(self.audit()['passed'])

    def test_boot_mismatch_refuses_baseline(self):
        self.baseline['boot_id'] = 'old'
        with self.assertRaisesRegex(ValueError, 'another boot'):
            self.audit()

    def test_cleanup_removes_only_exact_owned_socket(self):
        socket = self.root/'short'
        socket.mkdir()
        owned = socket/'aB'
        owned.mkdir()
        (owned/'fixture-output').write_text('private output')
        current = owned.stat()
        identity = dict(path=str(owned), boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                        dev=current.st_dev, ino=current.st_ino, uid=current.st_uid)
        metadata = dict(socket_root=str(owned), socket_identity=identity)
        profile = dict(short_root=str(socket))
        import json
        def write_once(path, value):
            with path.open('x') as stream:
                stream.write(json.dumps(value))
        protocol = types.SimpleNamespace(
            require=lambda condition,message: None if condition else (_ for _ in ()).throw(ValueError(message)),
            require_native_attempt=lambda root: None,
            read_attempt=lambda root: (metadata, {}, profile),
            load=lambda path: json.loads(path.read_text()),
            write_once=write_once)
        clean = dict(passed=True, live_owned_processes=[], unknown_unreadable_same_user_processes=[])
        with patch.dict(sys.modules, release_protocol=protocol), patch.object(ownership, 'inspect_processes', return_value=clean):
            result = ownership.audit_attempt(self.scope, remove_socket=True)
            self.assertTrue(result['socket_removed'])
            self.assertFalse(owned.exists())
            # Missing original is accepted only with its exact removal receipt.
            self.assertTrue(ownership.audit_attempt(self.scope, remove_socket=True)['socket_removed'])
            # A completed move/delete can be reconciled after a final-receipt interruption.
            (self.scope/'socket-removal.json').unlink()
            self.assertTrue(ownership.audit_attempt(self.scope, remove_socket=True)['socket_removed'])
            owned.symlink_to(self.root/'nonexistent')
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                ownership.audit_attempt(self.scope, remove_socket=True)
            self.assertTrue(owned.is_symlink())

    def test_blocked_audit_preserves_socket(self):
        socket = self.root/'aC'
        socket.mkdir()
        protocol = types.SimpleNamespace(require_native_attempt=lambda root: None,
            read_attempt=lambda root: (dict(socket_root=str(socket), socket_identity={}), {}, dict(short_root=str(self.root))))
        blocked = dict(passed=False, live_owned_processes=[dict(pid=123)], unknown_unreadable_same_user_processes=[])
        with patch.dict(sys.modules, release_protocol=protocol), patch.object(ownership, 'inspect_processes', return_value=blocked):
            self.assertFalse(ownership.audit_attempt(self.scope, remove_socket=True)['socket_removed'])
        self.assertTrue(socket.is_dir())

    def socket_fixture(self):
        socket = self.root/'aD'
        socket.mkdir()
        (socket/'data').write_text('original')
        info = socket.stat()
        return socket,dict(path=str(socket),boot_id='boot',dev=info.st_dev,ino=info.st_ino,uid=info.st_uid)

    def test_socket_replacement_is_preserved_in_quarantine(self):
        socket, identity = self.socket_fixture()
        original = ownership.os.rename
        def replace(source, destination, **kwargs):
            original(socket,self.root/'original-inode')
            socket.mkdir()
            (socket/'data').write_text('another owner')
            original(source,destination,**kwargs)
        with patch.object(ownership.os,'rename',side_effect=replace):
            with self.assertRaisesRegex(ValueError,'preserved in quarantine'):
                ownership._remove_socket(self.scope,socket,identity,protocol)
        intent = protocol.load(self.scope/'socket-removal-intent.json')
        self.assertEqual((Path(intent['quarantine'])/'owned/data').read_text(),'another owner')
        self.assertEqual((self.root/'original-inode/data').read_text(),'original')
        self.assertFalse((self.scope/'socket-removal.json').exists())

    def test_cleanup_resumes_after_rename_or_delete_interruption(self):
        socket, identity = self.socket_fixture()
        original = ownership.os.rename
        def interrupt(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('interrupted after rename')
        with patch.object(ownership.os,'rename',side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError,'after rename'):
                ownership._remove_socket(self.scope,socket,identity,protocol)
        original_delete = ownership.shutil.rmtree
        def interrupt_delete(*args,**kwargs):
            original_delete(*args,**kwargs)
            raise RuntimeError('interrupted after delete')
        interrupt_delete.avoids_symlink_attacks = True
        with patch.object(ownership.shutil,'rmtree',interrupt_delete):
            with self.assertRaisesRegex(RuntimeError,'after delete'):
                ownership._remove_socket(self.scope,socket,identity,protocol)
        ownership._remove_socket(self.scope,socket,identity,protocol)
        self.assertTrue(protocol.load(self.scope/'socket-removal.json')['removed'])
        self.assertFalse(socket.exists())

    def test_unrecorded_quarantine_is_preserved_and_retry_allocates_new_one(self):
        socket, identity = self.socket_fixture()
        original = Path.mkdir
        def interrupted(path,*args,**kwargs):
            original(path,*args,**kwargs)
            if path.name.startswith('.release-cleanup-'):
                raise RuntimeError('interrupted after mkdir')
        with patch.object(Path,'mkdir',interrupted):
            with self.assertRaisesRegex(RuntimeError,'after mkdir'):
                ownership._remove_socket(self.scope,socket,identity,protocol)
        first = Path(protocol.load(self.scope/'socket-removal-intent.json')['quarantine'])
        ownership._remove_socket(self.scope,socket,identity,protocol)
        self.assertTrue(first.is_dir())
        self.assertEqual(list(first.iterdir()),[])
        self.assertEqual(len(list((self.scope/'socket-cleanup-retries').glob('*-intent.json'))),1)
        self.assertFalse(socket.exists())

    def test_cleanup_cli_seals_absolute_and_relative_report_paths(self):
        from test_release_protocol import ReleaseProtocolTests
        fixture = ReleaseProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for absolute in (True, False):
            with self.subTest(absolute=absolute):
                root = fixture.attempt()
                metadata, _, _ = protocol.read_attempt(root)
                def clean(*args, **kwargs):
                    Path(metadata['socket_root']).rmdir()
                    return dict(passed=True,outcome='clean',owned_remaining=[],unknown=[],
                                socket_identity=metadata['socket_identity'],socket_removed=True)
                output = root/'outputs/final-cleanup.json' if absolute else Path('outputs/final-cleanup.json')
                argv = ['release_ownership.py','cleanup','--attempt',str(root),'--output',str(output)]
                with contextlib.chdir(root), patch.object(sys,'argv',argv), \
                     patch.object(ownership,'audit_attempt',side_effect=clean), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ownership.main(),0)
                proof = protocol._socket_cleanup_proof(root)
                self.assertEqual(proof['report'],'outputs/final-cleanup.json')

    def test_actual_deleted_cwd_child_and_orphan_stay_owned(self):
        child = '''import json,os,select,sys
directory=sys.stdin.readline().strip();os.chdir(directory)
print(json.dumps({'pid':os.getpid(),'ppid':os.getppid()}),flush=True)
select.select([sys.stdin],[],[],15);sys.stdin.readline()
'''
        orphan = 'import subprocess,sys;subprocess.Popen([sys.executable,"-B","-c",'+repr(child)+'],stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr,start_new_session=True)'
        for kind, program in [('child', child), ('orphan', orphan)]:
            with self.subTest(kind=kind):
                cwd = self.scope/kind
                cwd.mkdir()
                baseline = ownership.capture_baseline([self.scope], self.scope)
                proc = subprocess.Popen([sys.executable, '-B', '-c', program],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, cwd=self.root, env={'PATH':'/usr/bin:/bin'})
                identity = None
                try:
                    if kind == 'orphan':
                        self.assertEqual(proc.wait(timeout=5), 0)
                    proc.stdin.write(str(cwd)+'\n')
                    proc.stdin.flush()
                    self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
                    import json
                    identity = json.loads(proc.stdout.readline())
                    before = ownership.inspect_processes([self.scope], baseline, scope=self.scope)
                    self.assertIn(identity['pid'], [r['pid'] for r in before['live_owned_processes']])
                    cwd.rmdir()
                    after = ownership.inspect_processes([self.scope], baseline, scope=self.scope)
                    self.assertIn(identity['pid'], [r['pid'] for r in after['live_owned_processes']])
                    self.assertFalse(after['passed'])
                finally:
                    try:
                        proc.stdin.write('\n')
                        proc.stdin.flush()
                    except BrokenPipeError:
                        pass
                    proc.stdin.close()
                    proc.wait(timeout=5)
                    if identity:
                        path = Path('/proc')/str(identity['pid'])/'stat'
                        deadline = time.monotonic()+5
                        while path.exists() and time.monotonic()<deadline:
                            try:
                                if path.read_text().rsplit(')',1)[1].split()[0]=='Z':
                                    break
                            except FileNotFoundError:
                                break
                            time.sleep(0.01)
                        self.assertTrue(not path.exists() or path.read_text().rsplit(')',1)[1].split()[0]=='Z')
                    proc.stdout.close()
                    proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
