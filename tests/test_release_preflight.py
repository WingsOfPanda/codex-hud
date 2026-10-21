"""Catch release preparation failures before an expensive build or test stage."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import test_packaging as packaging


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/release-preflight.py'


class ReleasePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cp-pf-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()

    def run_preflight(self, spec, *, umask=None):
        config = self.root / 'input.json'
        config.write_text(json.dumps({'schema_version': 1} | spec))
        report = self.root / 'report.json'
        report.unlink(missing_ok=True)
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), str(config),
                                 '--scratch-parent', str(self.scratch), '--output', str(report)],
                                text=True, capture_output=True, umask=umask if umask is not None else -1)
        self.assertTrue(report.is_file(), result.stderr)
        value = json.loads(report.read_text())
        self.assertEqual(list(self.scratch.iterdir()), [], 'Owned scratch must be removed even on failure')
        self.assertEqual(result.returncode, 0 if value['passed'] else 1)
        return value

    def import_spec(self, symbol='selected', origin=None):
        module = self.root / 'release_fixture.py'
        module.write_text('selected = object()\n')
        return dict(python_path=[str(self.root)], imports=[dict(module='release_fixture',
                    origin=str(origin or module), symbols=[symbol])])

    def test_exact_interpreter_import_symbol_and_origin(self):
        value = self.run_preflight(self.import_spec())
        self.assertTrue(value['passed'])
        self.assertEqual(Path(value['python']['executable']).resolve(), Path(sys.executable).resolve())
        self.assertEqual(value['checks']['imports']['modules'][0]['origin'], str(self.root / 'release_fixture.py'))
        self.assertFalse((self.root / '__pycache__').exists())
        spec = self.import_spec()
        spec['imports'][0]['symbols'] = []
        self.assertTrue(self.run_preflight(spec)['passed'])

    def test_wrong_origin_module_is_not_executed(self):
        spec = self.import_spec(origin=self.root / 'reviewed/release_fixture.py')
        marker = self.root / 'wrong-module-executed'
        (self.root / 'release_fixture.py').write_text(
            'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\nselected = object()\n')
        value = self.run_preflight(spec)
        self.assertFalse(value['passed'])
        self.assertIn('origin', value['checks']['imports']['error'])
        self.assertFalse(marker.exists())

    def test_missing_symbol_and_wrong_origin_are_rejected(self):
        for spec, message in [(self.import_spec('stale_name'), 'stale_name'),
                              (self.import_spec(origin=self.root / 'stale.py'), 'origin')]:
            with self.subTest(message=message):
                value = self.run_preflight(spec)
                self.assertFalse(value['passed'])
                self.assertIn(message, value['checks']['imports']['error'])

    def test_missing_dependency_and_import_exit_are_reported(self):
        for body, message in [('from missing_release_dependency import selected', 'ModuleNotFoundError'),
                              ('raise SystemExit(2)', 'SystemExit')]:
            spec = self.import_spec()
            (self.root / 'release_fixture.py').write_text(body)
            value = self.run_preflight(spec)
            self.assertFalse(value['passed'])
            self.assertIn(message, value['checks']['imports']['error'])

    def staged_spec(self):
        staged = self.root / 'staged'
        staged.mkdir()
        (staged / 'controller.py').write_text('reviewed controller\n')
        digest = hashlib.sha256((staged / 'controller.py').read_bytes()).hexdigest()
        return dict(staged_inputs=dict(root=str(staged), files={'controller.py': digest}))

    def test_exact_staged_inventory_is_accepted(self):
        value = self.run_preflight(self.staged_spec())
        self.assertTrue(value['passed'])
        self.assertEqual(value['checks']['staged_inputs']['files'], 1)

    def test_omitted_tampered_extra_and_escaping_inputs_are_rejected(self):
        spec = self.staged_spec()
        staged = self.root / 'staged'
        path = staged / 'controller.py'
        original = path.read_bytes()
        mutations = [lambda: path.unlink(), lambda: path.write_text('tampered'),
                     lambda: (staged / 'omitted-from-manifest.py').write_text('extra'),
                     lambda: spec['staged_inputs']['files'].update({'../outside': '0' * 64})]
        for mutate in mutations:
            path.write_bytes(original)
            (staged / 'omitted-from-manifest.py').unlink(missing_ok=True)
            mutate()
            value = self.run_preflight(spec)
            self.assertFalse(value['passed'])
            self.assertIn('error', value['checks']['staged_inputs'])

    def test_symlinked_staged_file_is_rejected(self):
        spec = self.staged_spec()
        path = self.root / 'staged/controller.py'
        path.rename(self.root / 'outside')
        path.symlink_to(self.root / 'outside')
        self.assertFalse(self.run_preflight(spec)['passed'])

    def test_tampered_staged_code_is_not_imported(self):
        spec = self.staged_spec()
        path = self.root / 'staged/controller.py'
        marker = self.root / 'executed'
        path.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        spec.update(python_path=[str(path.parent)], imports=[dict(module='controller', origin=str(path), symbols=[])])
        value = self.run_preflight(spec)
        self.assertFalse(value['passed'])
        self.assertIn('not executed', value['checks']['imports']['error'])
        self.assertFalse(marker.exists())

    def test_socket_bind_at_107_bytes_and_cleanup(self):
        raw = '/tmp/' + 's' * 102
        value = self.run_preflight(dict(socket_paths=[dict(name='edge', path=raw)]))
        self.assertTrue(value['passed'])
        row = value['checks']['socket_paths']['sockets'][0]
        self.assertEqual(row['raw_bytes'], 107)
        self.assertEqual(row['bind_bytes'], 107)
        self.assertTrue(row['bound'])
        self.assertFalse(Path(row['bind_path']).exists())
        self.assertFalse(Path(raw).exists(), 'Do not bind or create the supplied path')

    def test_108_byte_raw_path_is_rejected_without_normalizing_dotdot(self):
        raw = '/tmp/' + 's' * 88 + '/a/../sock.sock'
        self.assertEqual(len(os.fsencode(raw)), 108)
        self.assertLess(len(os.fsencode(os.path.normpath(raw))), 108)
        value = self.run_preflight(dict(socket_paths=[dict(name='first-bind', path='/tmp/fixture.sock'),
                                                     dict(name='relative-home', path=raw)]))
        self.assertFalse(value['passed'])
        self.assertIn('108', value['checks']['socket_paths']['error'])

    def test_bind_failure_cleans_scratch_and_preserves_unrelated_file(self):
        unrelated = self.scratch / 'keep'
        unrelated.write_text('user data')
        # A long scratch prefix makes even the minimum owned bind exceed sun_path.
        parent = self.root / ('p' * 90)
        parent.mkdir()
        self.scratch = parent
        value = self.run_preflight(dict(socket_paths=[dict(name='short', path='/tmp/sock')]))
        self.assertFalse(value['passed'])
        self.assertEqual(unrelated.read_text(), 'user data')
        self.assertEqual(list(parent.iterdir()), [])

    def archive_spec(self):
        fixture = packaging.PackageIntegrityTests('test_valid_package_and_declared_hash_recipe')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        archive = self.root / 'trusted.tar.gz'
        packaging.package_release.write_archive(fixture.package, archive)
        return dict(archive=dict(path=str(archive), sha256=hashlib.sha256(archive.read_bytes()).hexdigest())), fixture

    def test_archive_modes_survive_umask077_and_strict_verifier(self):
        spec, fixture = self.archive_spec()
        before = hashlib.sha256(Path(spec['archive']['path']).read_bytes()).hexdigest()
        value = self.run_preflight(spec, umask=0o077)
        self.assertTrue(value['passed'], value)
        self.assertEqual(value['checks']['archive']['tree_sha256'], fixture.manifest['tree_sha256'])
        self.assertEqual(hashlib.sha256(Path(spec['archive']['path']).read_bytes()).hexdigest(), before)

    def test_archive_directory_modes_survive_umask077(self):
        spec, fixture = self.archive_spec()
        loader = importlib.util.spec_from_file_location('release_preflight', SCRIPT)
        preflight = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(preflight)
        previous = os.umask(0o077)
        try:
            value = preflight.check_archive(spec['archive'], self.scratch)
        finally:
            os.umask(previous)
        self.assertEqual(value['tree_sha256'], fixture.manifest['tree_sha256'])
        with tarfile.open(spec['archive']['path']) as archive:
            directories = [entry for entry in archive if entry.isdir()]
        self.assertGreater(len(directories), 1)
        for entry in directories:
            self.assertEqual((self.scratch / 'archive' / entry.name).stat().st_mode & 0o777, entry.mode)

    def test_archive_hash_and_manifest_corruption_are_rejected(self):
        spec, fixture = self.archive_spec()
        spec['archive']['sha256'] = '0' * 64
        self.assertFalse(self.run_preflight(spec)['passed'])
        (fixture.package / 'README.md').write_text('corrupt payload')
        archive = Path(spec['archive']['path'])
        packaging.package_release.write_archive(fixture.package, archive)
        spec['archive']['sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        value = self.run_preflight(spec)
        self.assertFalse(value['passed'])
        self.assertIn('mode mismatch', value['checks']['archive']['error'])

    def test_archive_traversal_link_and_duplicate_members_are_rejected(self):
        archive = self.root / 'invalid.tar'
        for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('package/link', tarfile.SYMTYPE), ('package/fifo', tarfile.FIFOTYPE),
                           ('package/duplicate', tarfile.REGTYPE)]:
            with self.subTest(name=name):
                with tarfile.open(archive, 'w') as tar:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    entry.linkname = '/tmp/outside'
                    tar.addfile(entry, io.BytesIO())
                    if name.endswith('duplicate'):
                        tar.addfile(entry, io.BytesIO())
                spec = dict(archive=dict(path=str(archive), sha256=hashlib.sha256(archive.read_bytes()).hexdigest()))
                self.assertFalse(self.run_preflight(spec)['passed'])
        self.assertFalse((self.root / 'escape').exists())

    def test_failures_are_readable_timed_and_do_not_hide_independent_checks(self):
        spec = self.import_spec('missing')
        spec.update(socket_paths=[dict(name='control', path='/tmp/socket')])
        value = self.run_preflight(spec)
        self.assertFalse(value['passed'])
        self.assertFalse(value['checks']['imports']['passed'])
        self.assertTrue(value['checks']['socket_paths']['passed'])
        self.assertGreaterEqual(value['checks']['imports']['seconds'], 0)

    def test_existing_report_is_not_overwritten(self):
        self.run_preflight(self.import_spec())
        report = self.root / 'report.json'
        original = report.read_bytes()
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), str(self.root / 'input.json'),
                                 '--output', str(report)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report.read_bytes(), original)

    def test_unknown_selection_is_rejected(self):
        value = self.run_preflight(dict(socket_path=[]))
        self.assertFalse(value['passed'])
        self.assertIn('unknown selection', value['error'])

    def test_boolean_schema_version_is_rejected(self):
        spec = self.import_spec()
        spec['schema_version'] = True
        self.assertFalse(self.run_preflight(spec)['passed'])

    def test_empty_import_and_socket_groups_are_rejected(self):
        for spec in [dict(imports=[]), dict(socket_paths=[]),
                     dict(imports=[], socket_paths=[dict(name='control', path='/tmp/socket')])]:
            with self.subTest(spec=spec):
                self.assertFalse(self.run_preflight(spec)['passed'])


if __name__ == '__main__':
    unittest.main()
