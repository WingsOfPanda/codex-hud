"""Bounded package conformance checks; no network, install or current-link changes."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mod_conformance', REPO / 'scripts/mod-conformance.py')
conformance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conformance)
NODE = Path(os.environ.get('MOD_TEST_NODE') or shutil.which('node') or '/node-unavailable').absolute()


def receipt(package):
    files = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in package.rglob('*') if path.is_file() and path.name != 'SOURCE.json'}
    (package / 'SOURCE.json').write_text(json.dumps(dict(schema=1, commit='a' * 40, files=files)))


def make_package(root, identifier='orbit-panel', version='1.0.0', **settings):
    package = root / identifier / version
    package.mkdir(parents=True)
    shutil.copy2(REPO / 'tests/fixtures/mod-host/view.mjs', package / 'view.mjs')
    (package / 'fixture.json').write_text(json.dumps(dict(label=f'{identifier}-{version}', **settings)))
    manifest = dict(id=identifier, version=version, host_contract=1, runtime=dict(node='>=22.18.0'),
                    entry='view.mjs', provides=['view.rows'], inputs=['session.identity'],
                    refresh=dict(min_interval_s=15, timeout_ms=300), ui=dict(rows=3), self_test='--self-test')
    (package / 'mod.json').write_text(json.dumps(manifest))
    receipt(package)
    return package


def change_manifest(package, **changes):
    path = package / 'mod.json'
    manifest = json.loads(path.read_text())
    manifest.update(changes)
    path.write_text(json.dumps(manifest))
    receipt(package)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mod-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = make_package(self.root)

    def reject(self, message):
        with self.assertRaisesRegex(conformance.ConformanceError, message):
            conformance.package_manifest(self.package)

    def test_digest_tampering_and_extra_payload_rejected(self):
        entry = self.package / 'view.mjs'
        entry.write_text(entry.read_text() + '\n// changed\n')
        self.reject('digest mismatch')
        receipt(self.package)
        (self.package / 'extra').write_text('extra')
        self.reject('closure')

    def test_receipt_missing_payload_and_traversal_rejected(self):
        (self.package / 'view.mjs').unlink()
        self.reject('closure')
        source = json.loads((self.package / 'SOURCE.json').read_text())
        source['files']['../outside'] = 'a' * 64
        (self.package / 'SOURCE.json').write_text(json.dumps(source))
        self.reject('unsafe receipt')

    def test_symlink_file_and_directory_rejected(self):
        for target in (self.package / 'view.mjs', self.root):
            with self.subTest(target=target):
                link = self.package / 'linked'
                link.symlink_to(target)
                self.reject('nonregular')
                link.unlink()

    def test_unknown_capabilities_and_unsupported_runtime_rejected(self):
        for changes, message in ((dict(provides=['view.rows', 'model.wake']), 'capabilities'),
                                 (dict(inputs=['filesystem.all']), 'capabilities'),
                                 (dict(runtime=dict(node='^22')), 'Node requirement'),
                                 (dict(id='another-panel'), 'ID differs'),
                                 (dict(host_contract=True), 'host contract'),
                                 (dict(version='2.0.0'), 'version differs'),
                                 (dict(entry='../outside'), 'receipt-bound')):
            original = (self.package / 'mod.json').read_bytes()
            with self.subTest(changes=changes):
                change_manifest(self.package, **changes)
                self.reject(message)
            (self.package / 'mod.json').write_bytes(original)
            receipt(self.package)

    def test_boolean_source_schema_rejected(self):
        path = self.package / 'SOURCE.json'
        source = json.loads(path.read_text())
        source['schema'] = True
        path.write_text(json.dumps(source))
        self.reject('SOURCE schema')

    def test_zero_refresh_and_row_requests_are_valid(self):
        change_manifest(self.package, refresh=dict(min_interval_s=0, timeout_ms=0), ui=dict(rows=0))
        manifest, _ = conformance.package_manifest(self.package)
        self.assertEqual(manifest['refresh'], dict(min_interval_s=0, timeout_ms=0))
        self.assertEqual(manifest['ui'], dict(rows=0))

    def test_negative_and_boolean_numeric_requests_are_rejected(self):
        original = (self.package / 'mod.json').read_bytes()
        for section, field in (('refresh', 'min_interval_s'), ('refresh', 'timeout_ms'), ('ui', 'rows')):
            for value in (-1, True, False):
                with self.subTest(section=section, field=field, value=value):
                    manifest = json.loads(original)
                    manifest[section][field] = value
                    change_manifest(self.package, **{section: manifest[section]})
                    self.reject('invalid refresh settings' if section == 'refresh' else 'invalid row budget')
                (self.package / 'mod.json').write_bytes(original)
                receipt(self.package)


@unittest.skipUnless(NODE.is_file(), 'set MOD_TEST_NODE to an installed absolute Node >=22.18')
class ExecutionTests(unittest.TestCase):
    setUp = PackageTests.setUp

    def check(self, **settings):
        if settings:
            path = self.package / 'fixture.json'
            current = json.loads(path.read_text())
            path.write_text(json.dumps(dict(current, **settings)))
            receipt(self.package)
        return conformance.check(self.package, NODE)

    def test_valid_stderr_does_not_fail_and_context_has_no_credentials(self):
        with patch.dict(os.environ, NODE_OPTIONS='--invalid', NODE_PATH='/untrusted',
                        OPENAI_API_KEY='must-not-inherit'):
            result = self.check(mode='stderr', trace=True, check_env=True)
        self.assertTrue(result['passed'])
        self.assertTrue(result['self_test'])
        self.assertEqual(result['stderr'], 'fixture diagnostic\n')
        self.assertNotIn('tmux_pane', result['request']['session'])

    def test_protocol_failures_are_explicit(self):
        for mode, message in (('malformed', 'invalid JSON'), ('wrong-id', 'identity differs'),
                              ('stdout-overflow', 'stdout overflow'), ('stderr-overflow', 'stderr overflow'),
                              ('exit', 'exited 7'), ('hang', 'timed out')):
            with self.subTest(mode=mode), self.assertRaisesRegex(conformance.ConformanceError, message):
                self.check(mode=mode)

    def test_self_test_failure_and_unmet_runtime_fail(self):
        with self.assertRaisesRegex(conformance.ConformanceError, 'exited 9'):
            self.check(self_test_exit=9)
        change_manifest(self.package, runtime=dict(node='>=99.0.0'))
        with self.assertRaisesRegex(conformance.ConformanceError, 'does not meet'):
            self.check()

    def assert_no_live_child(self, pid_file):
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = Path(f'/proc/{pid}/stat')
            if not status.exists() or status.read_text().split()[2] == 'Z':
                break
            time.sleep(.02)
        else:
            self.fail(f'owned grandchild {pid} survived timeout')

    def test_timeout_kills_owned_grandchild(self):
        pid_file = self.root / 'child.pid'
        with self.assertRaisesRegex(conformance.ConformanceError, 'timed out'):
            self.check(mode='grandchild', pid_file=str(pid_file))
        self.assert_no_live_child(pid_file)

    def test_exit_zero_parent_does_not_leave_live_grandchild(self):
        pid_file = self.root / 'child.pid'
        self.assertTrue(self.check(mode='orphan', pid_file=str(pid_file))['passed'])
        self.assert_no_live_child(pid_file)


class ReplyTests(unittest.TestCase):
    def test_bounds_types_and_metadata(self):
        reply = dict(host_contract=1, request_id='request', status='ok',
                     rows=[dict(text='summary', severity='normal')])
        self.assertEqual(conformance.validate_reply(json.dumps(reply), 'request'), reply)
        for changes in (dict(text='界' * 683), dict(age_ms=-1), dict(age_ms=True),
                        dict(share=1.1), dict(share=True), dict(severity='urgent')):
            with self.subTest(changes=changes), self.assertRaises(conformance.ConformanceError):
                row = dict(reply['rows'][0], **changes)
                conformance.validate_reply(json.dumps(dict(reply, rows=[row])), 'request')
        for changes in (dict(rows=reply['rows'] * 65), dict(overflow=-1), dict(status='empty'),
                        dict(diagnostics=[]), dict(host_contract=2), dict(host_contract=True)):
            with self.subTest(changes=changes), self.assertRaises(conformance.ConformanceError):
                conformance.validate_reply(json.dumps(dict(reply, **changes)), 'request')


if __name__ == '__main__':
    unittest.main()
