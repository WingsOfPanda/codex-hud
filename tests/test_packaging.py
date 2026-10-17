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
spec = importlib.util.spec_from_file_location('runtime_notices', ROOT / 'scripts/runtime-notices.py')
runtime_notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime_notices)


def elf(machine):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    header[6] = 1
    header[16:18] = (2).to_bytes(2, 'little')
    header[18:20] = machine.to_bytes(2, 'little')
    header[20:24] = (1).to_bytes(4, 'little')
    header[52:54] = (64).to_bytes(2, 'little')
    return bytes(header)


class PackageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name) / 'codex-plus-fixture'
        self.package.mkdir()
        for name in (package_release.DOCS +
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
        metadata = {'layoutVersion': 1, 'version': '0.163.0',
                    'target': 'x86_64-unknown-linux-gnu', 'variant': 'codex',
                    'entrypoint': 'bin/codex', 'resourcesDir': 'codex-resources',
                    'pathDir': 'codex-path'}
        (self.package / 'codex-package.json').write_text(json.dumps(metadata))
        sources = {'rust-runtime/' + name: 'c' * 64
                   for name in ('Cargo.toml', 'Cargo.lock', 'src/main.rs')}
        dependencies = {'schema_version': 1, 'target': 'x86_64-unknown-linux-gnu',
            'toolchain': {'rust': '1.99.0', 'library_notices_sha256':
                         package_release.sha256(self.package / package_release.RUNTIME_LICENSE_FILES[2])},
            'source_files': sources, 'cargo_lock_sha256': 'c' * 64,
            'source_sha256': package_release.tree_sha256(sources),
            'packages': [{'name': 'fixture', 'license': 'MIT'}]}
        (self.package / package_release.RUNTIME_LICENSE_FILES[1]).write_text(json.dumps(dependencies))
        self.manifest = {
            'schema_version': 1, 'release': package_release.RELEASE, 'plus_commit': 'a' * 40,
            'coordinator_protocol': package_release.COORDINATOR_PROTOCOL,
            'frontend_patch_sha256': 'b' * 64, 'target': 'x86_64-unknown-linux-gnu',
            'upstream': {'version': '0.163.0', 'tag': 'rust-v0.163.0',
                         'commit': package_release.UPSTREAM_COMMIT},
            'build': {'glibc': '2.39', 'rust': '1.99.0', 'cargo_profile': 'release'},
            'voice': False,
        }
        self.seal()

    def seal(self):
        if all((self.package / name).is_file() for name in
               ('bin/codex-plus-runtime',) + package_release.RUNTIME_LICENSE_FILES):
            self.manifest['runtime'] = package_release.runtime_provenance(
                self.package, self.manifest['target'])
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
        self.assertEqual(package_release.COORDINATOR_PROTOCOL, 2)
        self.assertFalse((self.package / 'codex-resources/zsh').exists())
        self.assertEqual(self.verify()['tree_sha256'], self.manifest['tree_sha256'])

    def test_coordinator_protocol_must_be_explicit_and_supported(self):
        for value in (None, True, 0, 1, '2'):
            with self.subTest(protocol=value):
                self.manifest['coordinator_protocol'] = value
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, 'coordinator compatibility'):
                    self.verify()

    def test_native_runtime_is_required_and_rust_sources_have_provenance(self):
        self.assertIn('rust-runtime/src/main.rs', package_release.SOURCE_FILES)
        self.assertIn('rust-runtime/Cargo.lock', package_release.SOURCE_FILES)
        self.assertFalse(any(name.startswith('codex_plus/') for name in package_release.SOURCE_FILES))
        self.assertIn('docs/advisor.md', package_release.DOCS)
        (self.package / 'bin/codex-plus-runtime').unlink()
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Missing regular package file: bin/codex-plus-runtime'):
            self.verify()

    def test_runtime_notices_required_and_provenance_cannot_disagree(self):
        self.manifest['runtime']['source_sha256'] = '0' * 64
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'Invalid native runtime provenance'):
            self.verify()
        (self.package / package_release.RUNTIME_LICENSE_FILES[0]).unlink()
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Missing regular package file'):
            self.verify()

    def test_legacy_protocol_one_observer_rejects_rust_package(self):
        from codex_plus.upgrade import select_package
        old = self.package.parent / 'previous'
        old.mkdir()
        previous = dict(self.manifest, release='0.163.0-plus.0.2.0', coordinator_protocol=1)
        (old / 'codex-plus.json').write_text(json.dumps(previous))
        (self.package.parent / 'current').symlink_to(self.package.name)
        with self.assertRaisesRegex(ValueError, 'incompatible coordinator package'):
            select_package(old)

    def test_corruption_missing_and_additional_files_rejected(self):
        for mutation in ('corrupt', 'missing', 'extra', 'mode'):
            with self.subTest(mutation=mutation):
                path = self.package / 'bin/codex-plus-runtime'
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
                path.chmod(0o755)
                (self.package / 'unexpected').unlink(missing_ok=True)

    def test_missing_required_resource_rejected_even_when_resealed(self):
        (self.package / 'codex-resources/bwrap').unlink()
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Missing regular package file'):
            self.verify()

    def test_wrong_architecture_rejected_even_when_resealed(self):
        (self.package / 'codex-path/rg').write_bytes(elf(183))
        self.seal()
        with self.assertRaisesRegex(ValueError, 'architecture'):
            self.verify()

    def test_native_runtime_architecture_and_mode_rejected_even_when_resealed(self):
        runtime = self.package / 'bin/codex-plus-runtime'
        runtime.write_bytes(elf(183))
        self.seal()
        with self.assertRaisesRegex(ValueError, 'architecture'):
            self.verify()
        runtime.write_bytes(elf(62))
        runtime.chmod(0o644)
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Wrong executable mode'):
            self.verify()

    def test_python_payload_rejected_even_when_resealed(self):
        (self.package / 'legacy.py').write_text('print("legacy")\n')
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Python runtime files'):
            self.verify()

    def test_native_runtime_rejects_sqlite_and_tls_shared_libraries(self):
        for name in ('libsqlite3.so.0', 'libssl.so.3', 'libcrypto.so.3'):
            with self.subTest(dependency=name), patch.object(package_release.subprocess, 'run') as run:
                run.return_value.stdout = f' (NEEDED) Shared library: [{name}]\n'
                with self.assertRaisesRegex(ValueError, 'Unsupported native runtime dependencies'):
                    package_release.validate_runtime_dependencies(self.package / 'bin/codex-plus-runtime')

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
        (self.root / 'rust-runtime/src/main.rs').write_text('uncommitted change\n')
        with self.assertRaisesRegex(ValueError, 'Source differs from Plus snapshot'):
            package_release.verify_sources(self.source, self.commit)

    def test_launcher_mode_change_rejected(self):
        (self.root / 'bin/codex-plus').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'modes differ'):
            package_release.verify_sources(self.source, self.commit)

    def test_added_or_deleted_rust_source_rejected(self):
        extra = self.root / 'rust-runtime/src/untracked.rs'
        extra.write_text('// untracked\n')
        with self.assertRaisesRegex(ValueError, 'Rust source inventory'):
            package_release.verify_sources(self.source, self.commit)
        extra.unlink()
        (self.root / 'rust-runtime/src/main.rs').unlink()
        with self.assertRaisesRegex(ValueError, 'Rust source inventory'):
            package_release.verify_sources(self.source, self.commit)

    def test_symlinked_rust_source_rejected(self):
        source = self.root / 'rust-runtime/src/main.rs'
        outside = self.root.parent / 'outside.rs'
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'Unsupported or excessive Rust source inventory'):
            package_release.verify_sources(self.source, self.commit)

    def test_unrelated_upstream_edit_and_untracked_source_rejected(self):
        (self.source / 'hud.rs').write_text('// unexpected\n')
        with self.assertRaisesRegex(ValueError, 'exact frontend'):
            package_release.verify_sources(self.source, self.commit)
        (self.source / 'hud.rs').write_text('// frontend\n')
        (self.source / 'extra.rs').write_text('// unexpected\n')
        with self.assertRaisesRegex(ValueError, 'untracked'):
            package_release.verify_sources(self.source, self.commit)


class RuntimeNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        (self.root / 'rust-runtime/src').mkdir(parents=True)
        (self.root / 'rust-runtime/src/main.rs').write_text('fn main() {}\n')
        (self.root / 'LICENSE').write_bytes((ROOT / 'LICENSE').read_bytes())
        self.packages, self.nodes, self.locked = [], [], []
        self.metadata = {'packages': self.packages,
                         'resolve': {'root': 'local-project', 'nodes': self.nodes}}
        self.add_package('codex-plus-runtime', '0.3.0', identity='local-project', local=True)
        self.add_package('dependency', '1.0.0')
        self.add_package('dev-only', '1.0.0', kind='dev')
        self.add_package('unreachable', '1.0.0', selected=False)

    def add_package(self, name, version, identity=None, local=False, kind=None, selected=True):
        directory = (self.root / 'rust-runtime' if local else
                     Path(self.temp.name) / 'cargo cache' / name)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / 'Cargo.toml'
        manifest.write_text(f'[package]\nname="{name}"\nversion="{version}"\nlicense="MIT"\n')
        if not local:
            (directory / 'LICENSE').write_bytes((ROOT / 'LICENSE').read_bytes())
        identity = identity or name
        source = None if local else 'registry+https://github.com/rust-lang/crates.io-index'
        package = {'id': identity, 'name': name, 'version': version, 'source': source,
                   'manifest_path': str(manifest), 'license': 'MIT', 'license_file': None,
                   'repository': 'https://example.invalid/shared-source'}
        self.packages.append(package)
        self.nodes.append({'id': identity, 'deps': []})
        self.locked.append({'name': name, 'version': version, 'source': source})
        if not local and selected:
            self.nodes[0]['deps'].append({'pkg': identity, 'dep_kinds': [{'kind': kind}]})
        lock = 'version = 4\n'
        for entry in self.locked:
            lock += f'\n[[package]]\nname = "{entry["name"]}"\nversion = "{entry["version"]}"\n'
            if entry['source']:
                lock += f'source = "{entry["source"]}"\nchecksum = "' + 'a' * 64 + '"\n'
        (self.root / 'rust-runtime/Cargo.lock').write_text(lock)
        return package, directory

    def collect(self):
        return runtime_notices.collect(self.root, 'x86_64-unknown-linux-gnu', self.metadata,
                                       'rustc 1.99.0 (fixture)', b'Rust library notices\n')

    def test_only_reachable_non_dev_packages_and_no_host_paths_are_recorded(self):
        bundle, metadata = self.collect()
        self.assertEqual([package['name'] for package in metadata['packages']],
                         ['codex-plus-runtime', 'dependency'])
        self.assertIn(b'Permission is hereby granted', bundle)
        self.assertNotIn(self.temp.name, json.dumps(metadata))
        self.assertEqual(metadata['cargo_lock_sha256'],
                         package_release.sha256(self.root / 'rust-runtime/Cargo.lock'))
        self.assertEqual(metadata['source_sha256'], package_release.tree_sha256(metadata['source_files']))
        self.packages.reverse()
        self.nodes.reverse()
        self.assertEqual(self.collect(), (bundle, metadata))

    def test_nested_notices_and_missing_required_license_terms(self):
        directory = Path(self.packages[1]['manifest_path']).parent
        nested = directory / 'vendor/NOTICE'
        nested.parent.mkdir()
        nested.write_text('Vendored component copyright\n')
        bundle, metadata = self.collect()
        self.assertIn(b'Vendored component copyright', bundle)
        self.assertIn('vendor/NOTICE', metadata['packages'][1]['license_files'])
        self.packages[1]['license'] = 'MIT AND Unicode-3.0'
        with self.assertRaisesRegex(ValueError, 'Missing applicable license text for dependency 1.0.0'):
            self.collect()
        self.packages[1]['license'] = 'MIT'
        (directory / 'LICENSE').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing applicable license text for dependency 1.0.0'):
            self.collect()

    def test_relative_license_pointer_needs_unique_selected_same_source_text(self):
        directory = Path(self.packages[1]['manifest_path']).parent
        (directory / 'LICENSE').write_text('../LICENSE\n')
        with self.assertRaisesRegex(ValueError, 'Unresolved license pointer for dependency 1.0.0'):
            self.collect()
        self.add_package('workspace-root', '1.0.0')
        bundle, metadata = self.collect()
        self.assertIn(b'Permission is hereby granted', bundle)
        dependency = next(package for package in metadata['packages'] if package['name'] == 'dependency')
        resolved = dependency['license_text_sources']['LICENSE']
        self.assertEqual(resolved['package'], 'workspace-root')
        self.assertEqual(resolved['sha256'], package_release.sha256(ROOT / 'LICENSE'))


if __name__ == '__main__':
    unittest.main()
