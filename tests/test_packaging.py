"""A release must reject corrupt, incomplete, or mislabeled installed payloads."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_release', ROOT / 'scripts/package-release.py')
package_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_release)


def elf(machine):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    header[18:20] = machine.to_bytes(2, 'little')
    return bytes(header)


class PackageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name) / 'codex-plus-fixture'
        self.package.mkdir()
        for name in (package_release.HELPERS + package_release.DOCS +
                     package_release.LICENSE_FILES + ('bin/codex-plus',)):
            path = self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture\n')
            path.chmod(0o755 if name == 'bin/codex-plus' else 0o644)
        for name in package_release.EXECUTABLES:
            path = self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(elf(62))
            path.chmod(0o755)
        bwrap_hash = hashlib.sha256(elf(62)).hexdigest().encode()
        (self.package / 'bin/codex').write_bytes(elf(62) + bwrap_hash)
        metadata = {'layoutVersion': 1, 'version': '0.162.1',
                    'target': 'x86_64-unknown-linux-gnu', 'variant': 'codex',
                    'entrypoint': 'bin/codex', 'resourcesDir': 'codex-resources',
                    'pathDir': 'codex-path'}
        (self.package / 'codex-package.json').write_text(json.dumps(metadata))
        self.manifest = {
            'schema_version': 1, 'release': package_release.RELEASE, 'plus_commit': 'a' * 40,
            'coordinator_protocol': package_release.COORDINATOR_PROTOCOL,
            'frontend_patch_sha256': 'b' * 64, 'target': 'x86_64-unknown-linux-gnu',
            'upstream': {'version': '0.162.1', 'tag': 'rust-v0.162.1',
                         'commit': package_release.UPSTREAM_COMMIT},
            'build': {'glibc': '2.39', 'rust': '1.99.0', 'cargo_profile': 'release'},
            'voice': False,
        }
        self.seal()

    def seal(self):
        files = package_release.tree_files(self.package)
        self.manifest['files'] = files
        canonical = json.dumps(files, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        self.manifest['tree_sha256'] = hashlib.sha256(canonical.encode('ascii')).hexdigest()
        self.save_manifest()

    def save_manifest(self):
        (self.package / 'codex-plus.json').write_text(json.dumps(self.manifest))

    def verify(self):
        return package_release.verify_package(self.package)

    def test_valid_package_and_declared_hash_recipe(self):
        from codex_plus.upgrade import COORDINATOR_PROTOCOL
        self.assertEqual(package_release.COORDINATOR_PROTOCOL, COORDINATOR_PROTOCOL)
        self.assertEqual(self.verify()['tree_sha256'], self.manifest['tree_sha256'])

    def test_coordinator_protocol_must_be_explicit_and_supported(self):
        for value in (None, True, 0, 2, '1'):
            with self.subTest(protocol=value):
                self.manifest['coordinator_protocol'] = value
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, 'coordinator compatibility'):
                    self.verify()

    def test_corruption_missing_and_additional_files_rejected(self):
        for mutation in ('corrupt', 'missing', 'extra', 'mode'):
            with self.subTest(mutation=mutation):
                path = self.package / 'codex_plus/rpc.py'
                original = path.read_bytes()
                if mutation == 'corrupt':
                    path.write_text('corrupted')
                elif mutation == 'missing':
                    path.unlink()
                elif mutation == 'extra':
                    (self.package / 'unexpected').write_text('extra')
                else:
                    path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, 'inventory'):
                    self.verify()
                path.write_bytes(original)
                path.chmod(0o644)
                (self.package / 'unexpected').unlink(missing_ok=True)

    def test_missing_required_resource_rejected_even_when_resealed(self):
        (self.package / 'codex-resources/zsh/bin/zsh').unlink()
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Missing regular package file'):
            self.verify()

    def test_wrong_architecture_rejected_even_when_resealed(self):
        (self.package / 'codex-path/rg').write_bytes(elf(183))
        self.seal()
        with self.assertRaisesRegex(ValueError, 'architecture'):
            self.verify()

    def test_voice_requires_explicit_false_and_no_payload(self):
        for voice in (None, True, 0):
            with self.subTest(voice=voice):
                self.manifest['voice'] = voice
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, 'voice=false'):
                    self.verify()
        del self.manifest['voice']
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'voice=false'):
            self.verify()
        self.manifest['voice'] = False
        (self.package / 'codex-resources/voice').mkdir()
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Voice must be omitted'):
            self.verify()

    def test_wrong_bwrap_digest_rejected_even_when_resealed(self):
        (self.package / 'codex-resources/bwrap').write_bytes(elf(62) + b'changed')
        self.seal()
        with self.assertRaisesRegex(ValueError, 'bwrap SHA-256'):
            self.verify()

    def test_tree_hash_and_symlinks_rejected(self):
        self.manifest['tree_sha256'] = '0' * 64
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'tree SHA-256'):
            self.verify()
        (self.package / 'alias').symlink_to('bin/codex')
        with self.assertRaisesRegex(ValueError, 'Unsupported package entry'):
            self.verify()

    def test_archive_preserves_layout_and_normalizes_metadata(self):
        archive = Path(self.temp.name) / 'release.tar.gz'
        package_release.write_archive(self.package, archive)
        digest = archive.read_bytes()
        (self.package / 'README.md').touch()
        package_release.write_archive(self.package, archive)
        self.assertEqual(archive.read_bytes(), digest)
        with tarfile.open(archive) as tar:
            executable = tar.getmember('codex-plus-fixture/bin/codex')
            self.assertEqual((executable.mode, executable.uid, executable.gid, executable.mtime),
                             (0o755, 0, 0, 0))
            self.assertTrue(all(not member.name.startswith('/') for member in tar))
        self.assertEqual(archive.with_suffix('.gz.sha256').read_text(),
                         hashlib.sha256(digest).hexdigest() + '  release.tar.gz\n')


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'plus'
        self.source = Path(self.temp.name) / 'upstream'
        for root in (self.root, self.source):
            root.mkdir()
            self.git(root, 'init', '-q')
            self.git(root, 'config', 'user.name', 'Release fixture')
            self.git(root, 'config', 'user.email', 'release@example.invalid')
        (self.source / 'hud.rs').write_text('// upstream\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-qm', 'upstream')
        pin = self.git(self.source, 'rev-parse', 'HEAD').decode().strip()
        (self.source / 'hud.rs').write_text('// frontend\n')
        frontend_patch = self.git(self.source, 'diff', 'HEAD')
        for name in package_release.SOURCE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(frontend_patch if name == package_release.PATCH else b'fixture\n')
        (self.root / 'bin/codex-plus').chmod(0o755)
        self.git(self.root, 'add', '.')
        self.git(self.root, 'commit', '-qm', 'Plus snapshot')
        self.commit = self.git(self.root, 'rev-parse', 'HEAD').decode().strip()
        self.addCleanup(patch.stopall)
        patch.object(package_release, 'ROOT', self.root).start()
        patch.object(package_release, 'UPSTREAM_COMMIT', pin).start()

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True).stdout

    def test_exact_snapshot_and_frontend_source_accepted(self):
        package_release.verify_sources(self.source, self.commit)

    def test_dirty_helper_snapshot_rejected(self):
        (self.root / 'codex_plus/rpc.py').write_text('uncommitted change\n')
        with self.assertRaisesRegex(ValueError, 'Source differs from Plus snapshot'):
            package_release.verify_sources(self.source, self.commit)

    def test_launcher_mode_change_rejected(self):
        (self.root / 'bin/codex-plus').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'modes differ'):
            package_release.verify_sources(self.source, self.commit)

    def test_unrelated_upstream_edit_and_untracked_source_rejected(self):
        (self.source / 'hud.rs').write_text('// unexpected\n')
        with self.assertRaisesRegex(ValueError, 'exact frontend'):
            package_release.verify_sources(self.source, self.commit)
        (self.source / 'hud.rs').write_text('// frontend\n')
        (self.source / 'extra.rs').write_text('// unexpected\n')
        with self.assertRaisesRegex(ValueError, 'untracked'):
            package_release.verify_sources(self.source, self.commit)


if __name__ == '__main__':
    unittest.main()
