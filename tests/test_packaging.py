"""A release must reject corrupt, incomplete, or mislabeled installed payloads."""
import hashlib
import base64
import copy
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
            'toolchain': {'rust': '1.99.1', 'library_notices_sha256':
                         package_release.sha256(self.package / package_release.RUNTIME_LICENSE_FILES[2])},
            'source_files': sources, 'cargo_lock_sha256': 'c' * 64,
            'source_sha256': package_release.tree_sha256(sources),
            'packages': [{'name': 'fixture', 'license': 'MIT'}]}
        (self.package / package_release.RUNTIME_LICENSE_FILES[1]).write_text(json.dumps(dependencies))
        native_sources = {'codex-rs/' + name: 'd' * 64 for name in
                          ('Cargo.toml', 'Cargo.lock', 'cli/Cargo.toml', 'code-mode-host/Cargo.toml')}
        native = dict(dependencies, source_files=native_sources, cargo_lock_sha256='d' * 64,
                      source_sha256=package_release.tree_sha256(native_sources),
                      roots=['codex-cli', 'codex-code-mode-host'],
                      build_features={'default_features': True, 'features': []},
                      packages=[{'name': 'codex-cli'}, {'name': 'codex-code-mode-host'}])
        (self.package / package_release.NATIVE_LICENSE_FILES[1]).write_text(json.dumps(native))
        self.manifest = {
            'schema_version': 1, 'release': package_release.RELEASE, 'plus_commit': 'a' * 40,
            'coordinator_protocol': package_release.COORDINATOR_PROTOCOL,
            'frontend_patch_sha256': 'b' * 64, 'target': 'x86_64-unknown-linux-gnu',
            'upstream': {'version': '0.163.0', 'tag': 'rust-v0.163.0',
                         'commit': package_release.UPSTREAM_COMMIT},
            'build': {'glibc': '2.39', 'rust': '1.99.1', 'cargo_profile': 'release'},
            'voice': False,
        }
        self.seal()

    def seal(self):
        if all((self.package / name).is_file() for name in
               ('bin/codex-plus-runtime',) + package_release.RUNTIME_LICENSE_FILES):
            self.manifest['runtime'] = package_release.runtime_provenance(
                self.package, self.manifest['target'])
        if all((self.package / name).is_file() for name in package_release.NATIVE_LICENSE_FILES):
            self.manifest['native'] = package_release.native_provenance(
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

    def test_previous_toolchain_provenance_is_not_accepted_for_new_packages(self):
        self.manifest['build']['rust'] = '1.99.0'
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'Invalid build provenance'):
            self.verify()
        self.manifest['build']['rust'] = '1.99.1'
        metadata_path = self.package / package_release.RUNTIME_LICENSE_FILES[1]
        metadata = json.loads(metadata_path.read_text())
        metadata['toolchain']['rust'] = '1.99.0'
        metadata_path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'Invalid native runtime dependency provenance'):
            self.seal()

    def test_runtime_notices_required_and_provenance_cannot_disagree(self):
        self.manifest['runtime']['source_sha256'] = '0' * 64
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'Invalid native runtime provenance'):
            self.verify()
        (self.package / package_release.RUNTIME_LICENSE_FILES[0]).unlink()
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Missing regular package file'):
            self.verify()

    def test_cli_host_notices_required_and_roots_must_match(self):
        self.manifest['native']['source_sha256'] = '0' * 64
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'Invalid native CLI/host provenance'):
            self.verify()
        path = self.package / package_release.NATIVE_LICENSE_FILES[1]
        metadata = json.loads(path.read_text())
        metadata['roots'] = ['codex-cli']
        path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'Invalid native CLI/host dependency provenance'):
            self.seal()
        metadata['roots'] = ['codex-cli', 'codex-code-mode-host']
        path.write_text(json.dumps(metadata))
        (self.package / package_release.NATIVE_LICENSE_FILES[0]).unlink()
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Missing regular package file: licenses/codex-native-NOTICES'):
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
                                       'rustc 1.99.1 (fixture)', b'Rust library notices\n')

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

    def test_supplemental_text_is_checksum_pinned_and_has_url_provenance(self):
        package = self.packages[1]
        directory = Path(package['manifest_path']).parent
        (directory / 'LICENSE').unlink()
        data = (ROOT / 'LICENSE').read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        cache = self.root / 'license-cache'
        cache.mkdir()
        (cache / checksum).write_bytes(data)
        entry = {'path': 'LICENSE', 'sha256': checksum,
                 'url': 'https://example.invalid/pinned-revision/LICENSE',
                 'revision': 'f' * 40}
        supplement = {'dependency@1.0.0': {'source': package['source'],
                      'checksum': 'a' * 64, 'files': [entry]}}
        args = (self.root, 'x86_64-unknown-linux-gnu', self.metadata,
                'rustc 1.99.1 (fixture)', b'Rust notices')
        with patch.object(runtime_notices, 'urlopen', side_effect=AssertionError('warm cache')):
            bundle, provenance = runtime_notices.collect(
                *args, supplemental=supplement, license_cache=cache)
        self.assertIn(b'Permission is hereby granted', bundle)
        dependency = next(p for p in provenance['packages'] if p['name'] == 'dependency')
        self.assertEqual(dependency['license_text_sources']['supplemental/LICENSE'], entry)
        supplement['dependency@1.0.0']['checksum'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'Supplemental license source mismatch'):
            runtime_notices.collect(*args, supplemental=supplement, license_cache=cache)
        supplement['dependency@1.0.0']['checksum'] = 'a' * 64
        (cache / checksum).write_bytes(b'corrupt')
        with patch.object(runtime_notices, 'urlopen') as fetch:
            fetch.return_value.__enter__.return_value.read.return_value = b'wrong text'
            with self.assertRaisesRegex(ValueError, 'Supplemental license checksum mismatch'):
                runtime_notices.collect(*args, supplemental=supplement, license_cache=cache)

    def test_git_workspace_inherits_files_and_license_directories_within_checkout(self):
        package = self.packages[1]
        package['source'] = 'git+https://example.invalid/pinned#' + 'f' * 40
        directory = Path(package['manifest_path']).parent
        checkout = directory.parent
        (checkout / '.git').mkdir()
        (checkout / 'LICENSE').write_bytes((ROOT / 'LICENSE').read_bytes())
        (checkout / 'LICENSES').mkdir()
        (checkout / 'LICENSES/notice.txt').write_text('Inherited notice\n')
        (directory / 'LICENSE').unlink()
        (directory / 'LICENSE').symlink_to('../LICENSE')
        files = dict(runtime_notices.license_files(package, self.root))
        self.assertIn('../LICENSE', files)
        self.assertIn('../LICENSES/notice.txt', files)
        (directory / 'LICENSE').unlink()
        (directory / 'LICENSE').symlink_to(self.root / 'LICENSE')
        with self.assertRaisesRegex(ValueError, 'Invalid license file for dependency'):
            runtime_notices.license_files(package, self.root)


class ProductionNoticeGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'LICENSE').write_bytes((ROOT / 'LICENSE').read_bytes())
        names = ('cli', 'host', 'shared', 'enabled', 'disabled', 'dev-only',
                 'dev-leak', 'windows-only', 'build-only')
        (self.root / 'Cargo.toml').write_text(
            '[workspace]\nresolver="2"\nmembers=' + json.dumps(list(names)) +
            '\n[workspace.package]\nlicense="MIT"\n')
        dependencies = {
            'cli': '[dependencies]\nshared={path="../shared"}\n'
                   'enabled={path="../enabled",optional=true}\n'
                   'disabled={path="../disabled",optional=true}\n'
                   '[features]\ndefault=["dep:enabled"]\n'
                   '[dev-dependencies]\ndev-only={path="../dev-only"}\n'
                   'shared={path="../shared",features=["extra"]}\n'
                   '[target.\'cfg(windows)\'.dependencies]\n'
                   'windows-only={path="../windows-only"}\n',
            'host': '[dependencies]\nshared={path="../shared"}\n'
                    '[build-dependencies]\nbuild-only={path="../build-only"}\n',
            'shared': '[dependencies]\ndev-leak={path="../dev-leak",optional=true}\n'
                      '[features]\nextra=["dep:dev-leak"]\n',
        }
        for name in names:
            directory = self.root / name
            (directory / 'src').mkdir(parents=True)
            (directory / 'src/lib.rs').write_text('')
            (directory / 'Cargo.toml').write_text(
                f'[package]\nname="{name}"\nversion="1.0.0"\nlicense.workspace=true\n' +
                dependencies.get(name, ''))
        subprocess.run(['cargo', '+1.99.1', 'generate-lockfile', '--offline',
                        '--manifest-path', str(self.root / 'Cargo.toml')],
                       check=True, capture_output=True)

    def collect(self, target='x86_64-unknown-linux-gnu'):
        metadata, selected, features = runtime_notices.cargo_graph(
            self.root / 'Cargo.toml', target, ['cli', 'host'])
        return runtime_notices.collect(
            self.root, target, metadata, 'rustc 1.99.1 (fixture)', b'Rust notices',
            manifest=self.root / 'Cargo.toml', roots=['cli', 'host'],
            selected=selected, features=features)

    def test_virtual_workspace_production_roots_features_target_and_inherited_license(self):
        bundle, provenance = self.collect()
        self.assertEqual([p['name'] for p in provenance['packages']],
                         ['build-only', 'cli', 'enabled', 'host', 'shared'])
        self.assertEqual(provenance['roots'], ['cli', 'host'])
        self.assertEqual(provenance['build_features'], {'default_features': True, 'features': []})
        self.assertIn(b'Permission is hereby granted', bundle)
        self.assertNotIn(self.temp.name, json.dumps(provenance))
        self.assertTrue(all(p['license_files'] for p in provenance['packages']))
        _, windows = self.collect('x86_64-pc-windows-msvc')
        self.assertIn('windows-only', [p['name'] for p in windows['packages']])

    def test_missing_inherited_license_fails_visibly(self):
        (self.root / 'LICENSE').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing applicable license text for'):
            self.collect()

    def test_missing_cached_manifest_fails_visibly(self):
        metadata, selected, features = runtime_notices.cargo_graph(
            self.root / 'Cargo.toml', 'x86_64-unknown-linux-gnu', ['cli', 'host'])
        (self.root / 'shared/Cargo.toml').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing cached source for shared 1.0.0'):
            runtime_notices.collect(
                self.root, 'x86_64-unknown-linux-gnu', metadata,
                'rustc 1.99.1 (fixture)', b'Rust notices', manifest=self.root / 'Cargo.toml',
                roots=['cli', 'host'], selected=selected, features=features)


class DeclarationNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.crate = self.root / 'cache/dependency'
        self.crate.mkdir(parents=True)
        runtime = self.root / 'rust-runtime'
        (runtime / 'src').mkdir(parents=True)
        (runtime / 'Cargo.toml').write_text('[package]\nname="host"\nversion="1.0.0"\n')
        self.cache = self.root / 'license-cache'
        self.cache.mkdir()
        self.bodies = {'MIT': b'Permission is hereby granted, free of charge. FULL MIT BODY\n',
                       'Apache-2.0': b'Apache License Version 2.0. FULL APACHE BODY\n',
                       'CC-BY-3.0-US': b'FULL CREATIVE COMMONS ATTRIBUTION BODY\n'}
        self.addCleanup(patch.stopall)
        patch.object(runtime_notices, 'SPDX_LICENSES',
                     {key: hashlib.sha256(value).hexdigest() for key, value in self.bodies.items()},
                     create=True).start()
        self.archive = b'Verified published crate archive fixture'
        self.checksum = hashlib.sha256(self.archive).hexdigest()
        (self.cache / self.checksum).write_bytes(self.archive)
        self.package = {'id': 'dependency', 'name': 'dependency', 'version': '1.0.0',
                        'source': 'registry+https://github.com/rust-lang/crates.io-index',
                        'manifest_path': str(self.crate / 'Cargo.toml'), 'license': 'MIT',
                        'license_file': None, 'authors': ['Fixture authors']}
        self.metadata = {'packages': [self.package],
                         'resolve': {'root': 'dependency', 'nodes': [{'id': 'dependency', 'deps': []}]}}
        (runtime / 'Cargo.lock').write_text(
            'version=4\n[[package]]\nname="dependency"\nversion="1.0.0"\n'
            f'source="{self.package["source"]}"\nchecksum="{self.checksum}"\n')
        self.supplement = {'source': self.package['source'], 'checksum': self.checksum,
                           'basis': 'published-license-declaration', 'declared_license': 'MIT',
                           'selected_license': 'MIT', 'selected_license_file': 'canonical/MIT.txt',
                           'authors_metadata': ['Fixture authors'],
                           'archive': {'url': 'https://static.crates.io/crates/dependency/dependency-1.0.0.crate',
                                       'sha256': self.checksum},
                           'source_evidence': {}, 'required_files': [], 'files': []}
        self.declaration('MIT')
        self.local('README.md', b'Publisher declaration evidence\n', retain=True)
        self.local('component.dat', b'ICU blob: embedded notice', retain=False)
        self.local('schema.json', b'{"title":"Debug Adapter Protocol"}', retain=False)
        self.canonical('MIT')

    def declaration(self, expression):
        self.package['license'] = expression
        self.supplement['declared_license'] = expression
        content = ('[package]\nname="dependency"\nversion="1.0.0"\n'
                   f'license="{expression}"\nauthors=["Fixture authors"]\n').encode()
        self.local('Cargo.toml', content, retain=False)
        self.local('Cargo.toml.orig', content, retain=True)

    def local(self, name, data, retain):
        (self.crate / name).write_bytes(data)
        self.supplement['source_evidence'][name] = {'sha256': hashlib.sha256(data).hexdigest(),
                                                  'url': 'https://example.invalid/revision/' + name}
        if retain:
            self.entry('retained/' + name, data, 'source-evidence', local_path=name)

    def entry(self, name, data, kind, **fields):
        checksum = hashlib.sha256(data).hexdigest()
        (self.cache / checksum).write_bytes(data)
        entry = dict(path=name, sha256=checksum, kind=kind,
                     url='https://example.invalid/revision/' + name, **fields)
        self.supplement['files'] = [f for f in self.supplement['files'] if f['path'] != name] + [entry]
        self.supplement['required_files'] = [f for f in self.supplement['required_files'] if f['path'] != name]
        self.supplement['required_files'].append({'path': name, 'kind': kind, 'sha256': checksum})
        return entry

    def canonical(self, identifier):
        entry = self.entry('canonical/' + identifier + '.txt', self.bodies[identifier],
                           'canonical-license', license=identifier,
                           revision='31ba1a50e5397e00a304dbadc76531740e89ee48')
        entry['url'] = ('https://raw.githubusercontent.com/spdx/license-list-data/' +
                        entry['revision'] + '/text/' + identifier + '.txt')

    def remove(self, name):
        self.supplement['files'] = [entry for entry in self.supplement['files'] if entry['path'] != name]

    def collect(self):
        return runtime_notices.collect(
            self.root, 'x86_64-unknown-linux-gnu', self.metadata, 'rustc 1.99.1 (fixture)', b'',
            supplemental={'dependency@1.0.0': self.supplement}, license_cache=self.cache)

    def test_curated_declaration_retains_metadata_and_canonical_classification(self):
        bundle, provenance = self.collect()
        self.assertIn(b'CANONICAL SPDX STANDARD LICENSE TERMS', bundle)
        self.assertIn(b'Authors metadata (not a copyright notice)', bundle)
        self.assertEqual(provenance['packages'][0]['license_resolution']['selected_license'], 'MIT')

    def test_icu_terms_cannot_replace_selected_wrapper_mit(self):
        self.entry('ICU-LICENSE', b'UNICODE LICENSE V3. Permission is hereby granted, free of charge.',
                   'component-notices')
        self.remove('canonical/MIT.txt')
        with self.assertRaisesRegex(ValueError, 'selected license body'):
            self.collect()

    def test_fxhash_header_cannot_replace_selected_apache_body(self):
        self.declaration('Apache-2.0/MIT')
        self.supplement['selected_license'] = 'Apache-2.0'
        self.supplement['selected_license_file'] = 'canonical/Apache-2.0.txt'
        self.entry('retained/lib.rs', b'// Licensed under the Apache License, Version 2.0 or MIT.',
                   'retained-source')
        with self.assertRaisesRegex(ValueError, 'selected license body'):
            self.collect()

    def test_component_grant_cc_terms_and_attribution_are_independent_requirements(self):
        self.canonical('CC-BY-3.0-US')
        self.entry('DAP-License.txt', b'Microsoft split grant: documentation CC BY, code MIT.',
                   'component-grant')
        self.entry('DAP-attribution.txt', b'Debug Adapter Protocol, Microsoft. Unchanged except line endings.',
                   'component-attribution')
        for name in ('canonical/CC-BY-3.0-US.txt', 'DAP-License.txt', 'DAP-attribution.txt'):
            with self.subTest(missing=name):
                saved = copy.deepcopy(self.supplement)
                self.remove(name)
                with self.assertRaisesRegex(ValueError, 'required supplemental'):
                    self.collect()
                self.supplement = saved

    def test_declaration_selection_and_local_evidence_are_bound(self):
        for mutation in ('declaration', 'selection', 'blob', 'schema', 'header', 'archive', 'kind'):
            with self.subTest(mutation=mutation):
                saved = copy.deepcopy(self.supplement)
                if mutation == 'declaration':
                    self.supplement['declared_license'] = 'Apache-2.0'
                elif mutation == 'selection':
                    self.supplement['selected_license'] = 'Apache-2.0'
                elif mutation == 'archive':
                    self.supplement['archive']['sha256'] = '0' * 64
                elif mutation == 'kind':
                    self.supplement['files'][-1]['kind'] = 'component-notices'
                else:
                    key = {'blob': 'component.dat', 'schema': 'schema.json', 'header': 'README.md'}[mutation]
                    self.supplement['source_evidence'][key]['sha256'] = '0' * 64
                with self.assertRaises(ValueError):
                    self.collect()
                self.supplement = saved

    def test_canonical_identity_revision_url_and_hash_are_enforced(self):
        for field, value in (('license', 'Apache-2.0'), ('revision', '0' * 40),
                             ('url', 'https://example.invalid/MIT'), ('sha256', '0' * 64)):
            with self.subTest(field=field):
                saved = copy.deepcopy(self.supplement)
                self.supplement['files'][-1][field] = value
                with self.assertRaisesRegex(ValueError, 'canonical'):
                    self.collect()
                self.supplement = saved

    def test_base64_download_is_decoded_before_hash_and_cache(self):
        data = b'Complete upstream ICU license bundle\n'
        entry = self.entry('ICU-LICENSE', data, 'component-notices', encoding='base64')
        (self.cache / entry['sha256']).unlink()
        with patch.object(runtime_notices, 'urlopen') as fetch:
            fetch.return_value.__enter__.return_value.read.return_value = base64.b64encode(data)
            cold = self.collect()
            self.assertEqual(fetch.call_count, 1)
        with patch.object(runtime_notices, 'urlopen', side_effect=AssertionError('warm cache')):
            self.assertEqual(self.collect(), cold)
        self.assertEqual((self.cache / entry['sha256']).read_bytes(), data)

    def test_cached_payload_returns_the_verified_snapshot(self):
        data = self.bodies['MIT']
        entry = next(f for f in self.supplement['files'] if f['kind'] == 'canonical-license')
        read_bytes = Path.read_bytes

        def replace_after_read(path):
            snapshot = read_bytes(path)
            path.write_bytes(b'concurrent partial cache write')
            return snapshot

        with patch.object(Path, 'read_bytes', replace_after_read):
            self.assertEqual(runtime_notices.pinned_bytes(entry, self.cache), data)

    def test_source_archive_bytes_are_verified_before_declaration_use(self):
        (self.cache / self.checksum).write_bytes(b'corrupt archive')
        with patch.object(runtime_notices, 'urlopen') as fetch:
            fetch.return_value.__enter__.return_value.read.return_value = b'wrong published archive'
            with self.assertRaisesRegex(ValueError, 'Supplemental license checksum mismatch'):
                self.collect()


if __name__ == '__main__':
    unittest.main()
