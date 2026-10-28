"""Reviewed stock fixtures reject unknown or mismatched binary identities."""
import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import test_advisor_integration as advisor


class StockBinaryIdentityTests(unittest.TestCase):
    def identity(self, version, digest, module=advisor):
        binary = Mock(spec=Path)
        with patch.object(module.subprocess, 'check_output', return_value=version + '\n'), \
             patch.object(module.hashlib, 'sha256') as sha256:
            sha256.return_value.hexdigest.return_value = digest
            return module.official_stock_identity(binary)

    def test_reviewed_architectures_select_distinct_stock_pins(self):
        selected = {}
        for architecture in ('x86_64', 'aarch64', 'unsupported'):
            with patch('platform.machine', return_value=architecture):
                spec = importlib.util.spec_from_file_location('stock_identity_fixture', advisor.__file__)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if architecture == 'unsupported':
                    with self.assertRaisesRegex(AssertionError, 'unsupported stock fixture architecture'):
                        self.identity('codex-cli 0.165.0', '0' * 64, module)
                    continue
                selected[architecture] = module
                for version, digest in (('codex-cli 0.163.0', module.OFFICIAL_SHA256),
                                        ('codex-cli 0.165.0', module.OFFICIAL_165_SHA256)):
                    self.assertEqual(self.identity(version, digest, module), (version, digest))
        for architecture, other in (('x86_64', 'aarch64'), ('aarch64', 'x86_64')):
            with self.assertRaisesRegex(AssertionError, 'pinned official stock binary'):
                self.identity('codex-cli 0.165.0', selected[other].OFFICIAL_165_SHA256, selected[architecture])

    def test_reviewed_163_and_165_pairs_are_accepted(self):
        for version, digest in (('codex-cli 0.163.0', advisor.OFFICIAL_SHA256),
                                ('codex-cli 0.165.0', advisor.OFFICIAL_165_SHA256)):
            with self.subTest(version=version):
                self.assertEqual(self.identity(version, digest), (version, digest))

    def test_changed_hash_and_cross_version_hash_are_rejected(self):
        for version, digest in (('codex-cli 0.163.0', '0' * 64),
                                ('codex-cli 0.165.0', '0' * 64),
                                ('codex-cli 0.163.0', advisor.OFFICIAL_165_SHA256),
                                ('codex-cli 0.165.0', advisor.OFFICIAL_SHA256)):
            with self.subTest(version=version, digest=digest):
                with self.assertRaisesRegex(AssertionError, 'pinned official stock binary'):
                    self.identity(version, digest)

    def test_unreviewed_version_is_rejected_even_with_reviewed_hash(self):
        with self.assertRaisesRegex(AssertionError, 'reviewed official stock version'):
            self.identity('codex-cli 0.166.0', advisor.OFFICIAL_165_SHA256)

    def test_legacy_163_digest_override_is_still_honored(self):
        content = b'legacy release recipe stock fixture'
        digest = hashlib.sha256(content).hexdigest()
        binary = Mock(spec=Path)
        binary.read_bytes.return_value = content
        with patch.object(advisor, 'OFFICIAL_SHA256', digest), \
             patch.object(advisor.subprocess, 'check_output', return_value='codex-cli 0.163.0\n'):
            self.assertEqual(advisor.official_stock_identity(binary), ('codex-cli 0.163.0', digest))


if __name__ == '__main__':
    unittest.main()
