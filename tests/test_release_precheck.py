"""Allocation-free readiness; compiler/checkout observations are synthetic boundaries."""
import contextlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_release_native as native_tests
import test_packaging as packaging

sys.path.insert(0, str(native_tests.ROOT / 'scripts'))
import release_compat as compat


class Readiness(unittest.TestCase):
    def setUp(self):
        fixture = native_tests.NativeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.base, self.plan, self.profile = fixture.base, fixture.plan, fixture.profile
        sys.path.insert(0, str(compat.RECIPE))
        import workflow
        self.workflow, self.native = workflow, workflow.native
        build = self.profile['native_build']
        for name in ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache', 'license_cache'):
            Path(build[name]).mkdir()
        Path(build['official_binary']).write_bytes(b'official fixture')
        build['official_sha256'] = self.native.sha(build['official_binary'])
        build['pkg_config'] = fixture.libcap_fixture()
        self.plan['compat']['stock_binary_sha256']['x86_64'] = build['official_sha256']
        self.plan['compat']['fixture_files'] = {name: compat.digest(native_tests.ROOT / name)
                                               for name in compat.RECOVERY_FIXTURES}
        for gates in self.plan['gates'].values():
            gates['compat']['checks'] = list(compat.GATES)
            gates['captures']['checks'] = ['native-cursor-states']
        old = packaging.PackageIntegrityTests('test_valid_package_and_declared_hash_recipe')
        old.setUp()
        self.addCleanup(old.doCleanups)
        old.manifest.update(release=self.plan['previous_release']['tag'],
                            plus_commit=self.plan['compat']['previous_source_commit'])
        old.seal()
        self.old = old.package
        previous = self.plan['previous_release']['targets']['x86_64']
        previous.update(manifest_sha256=compat.digest(self.old / 'codex-plus.json'),
                        tree_sha256=old.manifest['tree_sha256'])
        self.hook = dict(argv=[self.profile['python']['path'], '-B',
            str(native_tests.ROOT / 'scripts/release_ownership.py'), 'audit',
            '--attempt', '{attempt}', '--output', '{report}'],
            cwd=str(native_tests.ROOT), env=self.native.python_env(self.profile), deadline_seconds=180)

    def observed(self, profile):
        self.native.fixture_import_probe(profile)
        return self.fixture.observed, {}

    def inventory(self):
        return {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_mode)
                for root in (self.base, self.old) for path in root.rglob('*')
                if path.is_file() and not path.is_symlink()}

    def call(self, *, bind_profile=True):
        if bind_profile:
            self.plan['targets']['x86_64']['profile_sha256'] = self.workflow.protocol.digest(self.profile)
        paths = []
        for name, value in (('plan', self.plan), ('profile', self.profile), ('hook', self.hook)):
            path = self.base / (name + '.json')
            path.write_text(json.dumps(value))
            paths.append(str(path))
        before = self.inventory()
        before_short = list(Path(self.profile['short_root']).iterdir())
        index = Path(subprocess.check_output(['git', '-C', str(native_tests.ROOT),
            'rev-parse', '--path-format=absolute', '--git-path', 'index'], text=True).strip())
        index_before = (index.read_bytes(), index.stat().st_ino, index.stat().st_mtime_ns)
        with patch.object(self.native, 'observe', side_effect=self.observed), \
             patch.object(self.native, 'input_closure', return_value={}), \
             patch.object(sys, 'argv', ['release_compat.py', 'precheck', '--plan', paths[0],
                 '--profile', paths[1], '--cleanup-hook', paths[2], '--old-package', str(self.old)]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            try:
                compat.main()
            finally:
                self.assertFalse(Path(self.profile['build_root']).exists())
                self.assertEqual(before_short, list(Path(self.profile['short_root']).iterdir()))
                self.assertEqual(before, self.inventory())
                self.assertEqual(index_before, (index.read_bytes(), index.stat().st_ino, index.stat().st_mtime_ns))
        return json.loads(output.getvalue())

    def test_native_readiness_passes_without_attempt_or_socket_allocation(self):
        result = self.call()
        self.assertTrue(result['ready'])
        self.assertEqual(result['scope'], 'native-prerequisites')
        self.assertIn('packaging-notices', result['not_checked'])
        self.assertNotIn('accepted', result)

    def test_old_package_content_and_modes_fail_without_allocation(self):
        helper = self.old / 'bin/codex-plus-runtime'
        original, mode = helper.read_bytes(), helper.stat().st_mode & 0o777
        for defect in ('content', 'mode'):
            with self.subTest(defect=defect):
                helper.write_bytes(original + b'changed' if defect == 'content' else original)
                helper.chmod(0o400 if defect == 'mode' else mode)
                with self.assertRaisesRegex(ValueError, 'inventory'):
                    self.call()
                helper.chmod(mode)
                helper.write_bytes(original)

    def test_fixture_import_and_recovery_pins_fail_before_allocation(self):
        imports = self.profile['python']['fixture_imports']
        pyte = imports.pop('pyte')
        with self.assertRaisesRegex(ValueError, 'Pin pyte'):
            self.call()
        imports['pyte'] = pyte
        pyte['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.call()
        pyte['sha256'] = compat.digest(pyte['origin'])
        fixtures = self.plan['compat']['fixture_files']
        name = compat.RECOVERY_FIXTURES[0]
        sha = fixtures.pop(name)
        with self.assertRaisesRegex(ValueError, 'Recovery fixture closure'):
            self.call()
        fixtures[name] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Wrong fixture bytes'):
            self.call()
        fixtures[name] = sha

    def test_system_pin_libcap_and_hook_fail_before_allocation(self):
        name = str(Path(self.native.SYSTEM_TOOLS[0]).resolve())
        sha = self.profile['system_tools'].pop(name)
        with self.assertRaisesRegex(ValueError, 'System tool'):
            self.call()
        self.profile['system_tools'][name] = sha
        library = Path(self.profile['native_build']['pkg_config']['path']).parent / 'lib/libcap.so.2'
        library.write_bytes(b'wrong architecture')
        with self.assertRaisesRegex(ValueError, 'libcap input hash mismatch'):
            self.call()
        self.profile['native_build']['pkg_config']['files'][str(library.with_name('libcap.so'))] = compat.digest(library)
        with self.assertRaisesRegex(ValueError, 'not native ELF'):
            self.call()

    def test_system_libcap_is_probed_without_a_private_override(self):
        del self.profile['native_build']['pkg_config']
        empty = self.base / 'empty-pkg-config'
        empty.mkdir()
        env = self.native.python_env(self.profile) | {'PKG_CONFIG_LIBDIR': str(empty)}
        with patch.object(self.native, 'python_env', return_value=env):
            with self.assertRaises(subprocess.CalledProcessError):
                self.call()

    def test_profile_and_observed_checkout_pins_fail_before_allocation(self):
        self.plan['targets']['x86_64']['profile_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'computed profile_sha256=' + self.workflow.protocol.digest(self.profile)):
            self.call(bind_profile=False)
        for name in ('plus_source_commit', 'tooling_commit'):
            original = self.fixture.observed[name]
            self.fixture.observed[name] = '0' * 40
            with self.assertRaisesRegex(ValueError, 'Checkout identity mismatch: ' + name):
                self.call()
            self.fixture.observed[name] = original
        self.fixture.observed['hostname'] = 'different-host'
        with self.assertRaisesRegex(ValueError, 'Native host mismatch: hostname'):
            self.call()

    def test_unsupported_compatibility_gate_fails_before_allocation(self):
        self.plan['gates']['x86_64']['compat']['checks'] = ['invented-gate']
        with self.assertRaisesRegex(ValueError, 'Unsupported compatibility checks'):
            self.call()

    def test_production_hook_cannot_hide_a_different_command(self):
        argv = self.hook['argv'][:]
        for command in ('/usr/bin/false', '/usr/bin/true'):
            with self.subTest(command=command):
                self.hook['argv'][0] = command
                with self.assertRaisesRegex(ValueError, 'common ownership audit'):
                    self.call()
        self.hook['argv'] = argv
        self.hook['env']['UNREVIEWED'] = 'override'
        with self.assertRaisesRegex(ValueError, 'common ownership audit'):
            self.call()

    def ap_setup(self):
        source = self.base / 'ap-source'
        (source / 'dist').mkdir(parents=True)
        (source / 'dist/ap.cjs').write_bytes(b'released AP bundle')
        (source / 'package.json').write_text(json.dumps({'version': self.plan['compat']['ap_version']}))
        modules = self.base / 'node_modules'
        for name in ('vitest/vitest.mjs', 'vitest/dist/index.js'):
            path = modules / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('export default 1')
        (source / 'node_modules').symlink_to(modules)
        (source / '.gitignore').write_text('node_modules\n')
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c',
            'user.email=fixture@example.invalid', 'commit', '-qm', 'AP fixture'], check=True)
        commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        bundle = self.base / 'installed/plugin/dist/ap.cjs'
        bundle.parent.mkdir(parents=True)
        bundle.write_bytes((source / 'dist/ap.cjs').read_bytes())
        registry = self.base / 'registry.json'
        registry.write_text(json.dumps({'plugins': {'fixture': [{'installPath': str(bundle.parent.parent),
            'version': self.plan['compat']['ap_version'], 'gitCommitSha': commit}]}}))
        executor = self.base / 'executor/bin'
        executor.mkdir(parents=True)
        (executor / 'codex-plus-runtime').write_bytes(b'executor fixture')
        node = Path('/usr/bin/true').resolve()
        libraries = {str(Path(line.split()[-1]).resolve()) for line in Path('/proc/self/maps').read_text().splitlines()
                     if 'libpython' in line and line.split()[-1].startswith('/')}
        self.profile.update(source_root=str(source), hostname=platform.node(), architecture=platform.machine(),
            executor_package_root=str(executor.parent), ap_replay={'node': str(node), 'node_modules': str(modules),
                'installed_bundle': str(bundle), 'installed_registry': str(registry)})
        self.profile['python']['libpython'] = next(iter(libraries), None)
        self.profile['system_tools'][str(node)] = compat.digest(node)
        self.plan['compat']['ap_replay'] = dict(source_commit=commit, installed_plugin_commit=commit,
            bundle_sha256=compat.digest(bundle))
        return source, bundle, modules

    def call_ap(self):
        paths = []
        for name, value in (('plan', self.plan), ('profile', self.profile)):
            path = self.base / (name + '.json')
            path.write_text(json.dumps(value))
            paths.append(str(path))
        before = self.inventory()
        checkout = self.native.checkout_identity
        def observed_checkout(root):
            if Path(root) == native_tests.ROOT:
                return {'head': self.plan['tooling_commit']}
            return checkout(root)
        with patch.object(self.native, 'checkout_identity', side_effect=observed_checkout), \
             patch.object(sys, 'argv', ['release_compat.py', 'precheck-ap', '--plan', paths[0], '--profile', paths[1]]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            try:
                compat.main()
            finally:
                self.assertFalse(Path(self.profile['build_root']).exists())
                self.assertEqual([], list(Path(self.profile['short_root']).iterdir()))
                self.assertEqual(before, self.inventory())
        return json.loads(output.getvalue())

    def test_ap_readiness_is_host_local_and_does_not_claim_native_qualification(self):
        self.ap_setup()
        result = self.call_ap()
        self.assertTrue(result['ready'])
        self.assertEqual(result['scope'], 'ap-prerequisites-only')
        self.assertIn('product-plan-native-qualification', result['not_checked'])

    def test_ap_bundle_node_and_modules_fail_before_allocation(self):
        source, bundle, modules = self.ap_setup()
        bundle.write_bytes(b'wrong bundle')
        with self.assertRaisesRegex(ValueError, 'bundle differs'):
            self.call_ap()
        bundle.write_bytes((source / 'dist/ap.cjs').read_bytes())
        node = self.profile['ap_replay']['node']
        self.profile['system_tools'][node] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Node executable'):
            self.call_ap()
        self.profile['system_tools'][node] = compat.digest(node)
        self.profile['ap_replay']['node_modules'] = str(self.base)
        with self.assertRaisesRegex(ValueError, 'module origin'):
            self.call_ap()
        self.profile['ap_replay']['node_modules'] = str(modules)
        (modules / 'escape').symlink_to(bundle)
        with self.assertRaisesRegex(ValueError, 'origin escapes'):
            self.call_ap()


if __name__ == '__main__':
    unittest.main()
