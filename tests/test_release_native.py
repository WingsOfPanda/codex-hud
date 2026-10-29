"""Real failures at the native runner boundary, with expensive bodies stubbed."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_release_protocol as core_tests

ROOT, protocol = core_tests.ROOT, core_tests.protocol

sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('release_native', ROOT / 'scripts/release_native.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeTests(unittest.TestCase):
    def setUp(self):
        fixture = core_tests.ReleaseProtocolTests()
        fixture.setUp(legacy=True)
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.base, self.profile, self.plan = fixture.base, fixture.profile, fixture.plan
        self.profile['python']['path'] = str(Path(sys.executable).resolve())
        self.profile['python']['version'] = sys.version.split()[0]
        sites = self.base / 'fixture-sites'
        imports = {}
        for name, body in (('pyte', 'class Screen: pass\nclass Stream: pass\n'),
                           ('wcwidth', 'def wcwidth(value): return 1\n')):
            origin = sites / name / '__init__.py'
            origin.parent.mkdir(parents=True)
            origin.write_text(body)
            imports[name] = {'origin': str(origin), 'sha256': native.sha(origin)}
        self.profile['python'].update(fixture_sites=[str(sites)], fixture_imports=imports)
        self.profile['native_build'] = {'upstream_repository': str(self.base / 'upstream'),
            'cargo_home': str(self.base / 'cargo'), 'cargo_target_dir': str(self.base / 'target'),
            'v8_cache': str(self.base / 'v8'), 'license_cache': str(self.base / 'licenses'),
            'cargo_jobs': 3, 'test_threads': 2, 'official_binary': str(self.base / 'official'),
            'official_sha256': 'd' * 64}
        self.profile['system_tools'] = {str(Path(path).resolve()): native.sha(Path(path).resolve())
                                        for path in native.SYSTEM_TOOLS}
        self.profile['short_root'] = str(self.base / 's')
        Path(self.profile['short_root']).mkdir()
        self.observed = {name: copy.deepcopy(self.profile[name]) for name in
                         ('hostname', 'architecture', 'toolchain', 'python', 'executor_package_root')}
        self.observed.update(plus_source_commit=self.plan['plus_source_commit'], tooling_commit=self.plan['tooling_commit'])
        self.plan['targets']['x86_64']['profile_sha256'] = protocol.digest(self.profile)
        self.hook = {'argv': [str(Path(sys.executable).resolve()), '-B', '-c',
            'import json,sys;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps('
            '{"outcome":"clean","owned_remaining":[],"unknown":[]}))', '{report}'],
            'cwd': str(ROOT), 'env': {}, 'deadline_seconds': 10}

    def attempt(self):
        return protocol.init_attempt(self.plan, self.profile, self.fixture.source,
                                     observed=self.observed, rehearsal=True)

    def test_recipe_source_is_pinned_0163_and_product_default_is_0165(self):
        self.assertEqual(self.plan['upstream'], {
            'version': '0.163.0', 'tag': 'rust-v0.163.0',
            'commit': '83086188ae0614bd882818ff1aa23f3194962012'})
        protocol.validate_plan(self.plan, self.fixture.source)
        product = core_tests.ReleaseProtocolTests()
        product.setUp()
        self.addCleanup(product.doCleanups)
        self.assertEqual(product.plan['upstream']['version'], '0.165.0')
        protocol.validate_plan(product.plan, product.source)
        with self.assertRaisesRegex(ValueError, 'Upstream recipe needs qualification'):
            native.build_inputs(product.plan, self.profile)

    def test_missing_legacy_source_object_fails_without_fetch_or_skip(self):
        import release_fixture_source as source
        error = subprocess.CalledProcessError(128, ['git', 'archive'], stderr=b'not a valid object name')
        with patch.object(source.subprocess, 'check_output', side_effect=error) as git:
            with self.assertRaisesRegex(RuntimeError, 'Pinned 0.163 fixture ' + source.LEGACY_COMMIT):
                source.legacy_archive.__wrapped__()
        self.assertEqual(git.call_count, 1)
        self.assertEqual(git.call_args.args[0][3:5], ['archive', source.LEGACY_COMMIT])

    def test_paired_build_inputs_bind_both_official_binaries_and_rustdoc(self):
        self.plan['upstream'] = native.UPSTREAM_165
        self.plan['previous_release']['upstream'] = native.UPSTREAM_163
        self.plan['compat']['recipe_revision'] = 2
        build = self.profile['native_build']
        for name in ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache', 'license_cache'):
            Path(build[name]).mkdir()
        Path(build['official_binary']).write_bytes(b'new official fixture')
        previous = self.base / 'previous-official'
        previous.write_bytes(b'old official fixture')
        build.update(official_sha256=native.sha(build['official_binary']),
                     previous_official_binary=str(previous), previous_official_sha256=native.sha(previous))
        self.plan['compat']['stock_binary_sha256']['x86_64'] = build['official_sha256']
        self.plan['compat']['previous_stock_binary_sha256'] = {'x86_64': native.sha(previous)}
        self.profile['toolchain']['rustc'] = str(self.base / 'rustc')
        rustdoc = Path(self.profile['toolchain']['rustc']).with_name('rustdoc')
        rustdoc.write_bytes(b'pinned rustdoc fixture')
        self.profile['system_tools'][str(rustdoc.resolve())] = native.sha(rustdoc)
        native.build_inputs(self.plan, self.profile)
        previous.write_bytes(b'changed previous official')
        with self.assertRaisesRegex(ValueError, 'Paired official binary pin mismatch'):
            native.build_inputs(self.plan, self.profile)
        previous.write_bytes(b'old official fixture')
        rustdoc.write_bytes(b'changed rustdoc')
        with self.assertRaisesRegex(ValueError, 'rustdoc'):
            native.build_inputs(self.plan, self.profile)
        rustdoc.write_bytes(b'pinned rustdoc fixture')
        del build['previous_official_sha256']
        with self.assertRaisesRegex(ValueError, 'Previous official binary'):
            native.build_inputs(self.plan, self.profile)

    def setup_runner(self, root):
        context = {'closure': {str(ROOT / 'scripts/release_native.py'):
                              protocol.file_record(ROOT / 'scripts/release_native.py')}, 'cleanup': self.hook}
        protocol.write_once(root / 'native-preparation.json', context)
        return context

    def libcap_fixture(self):
        root = self.base / 'libcap'
        pc = root / 'pkgconfig/libcap.pc'
        header = root / 'include/sys/capability.h'
        library = root / 'lib/libcap.so'
        for path in (pc, header, library):
            path.parent.mkdir(parents=True, exist_ok=True)
        header.write_text('/* pinned local libcap header */\n')
        target = library.with_name('libcap.so.2')
        target.write_bytes(b'\x7fELF\x02\x01' + b'\0' * 12 + (62).to_bytes(2, 'little'))
        library.symlink_to(target.name)
        pc.write_text(f'prefix={root}\nlibdir=${{prefix}}/lib\nincludedir=${{prefix}}/include\n'
                      'Name: libcap\nDescription: fixture libcap\nVersion: 2.66\n'
                      'Libs: -L${libdir} -lcap\nCflags: -I${includedir}\n')
        return {'path': str(pc.parent), 'files': {str(path): native.sha(path.resolve())
                                                for path in (pc, header, library)}}

    def fake_stage(self, argv, *, stdout, **kwargs):
        root = Path(argv[argv.index('--attempt') + 1])
        name = argv[argv.index('--stage') + 1]
        stdout.write(b'stage output\n')
        protocol.write_once(root / 'outputs' / (name + '.json'),
                            {'architecture': 'x86_64', 'plus_source_commit': self.plan['plus_source_commit']})
        return 0

    def test_path_derivation_and_child_only_marker(self):
        root = self.attempt()
        context = native.Context(root, 'runtime')
        self.assertTrue(context.UPSTREAM.is_relative_to(root))
        self.assertEqual(context.PACKAGE.name, 'codex-plus-' + self.plan['release'] + '-' + self.profile['triple'])
        self.assertEqual(context.ENV['CODEX_PLUS_RELEASE_ATTEMPT'], protocol.load(root / 'attempt.json')['attempt'])
        self.assertNotIn('CODEX_PLUS_RELEASE_ATTEMPT', native.python_env(self.profile))
        self.assertEqual(context.ENV['CARGO_BUILD_JOBS'], '3')
        self.assertEqual(context.ENV['RUST_TEST_THREADS'], '2')

    def test_scoped_loader_comes_from_profile(self):
        self.profile['python']['libpython'] = '/private/python/lib/libpython3.12.so.1.0'
        self.assertEqual(native.python_env(self.profile)['LD_LIBRARY_PATH'], '/private/python/lib')
        self.profile['python']['libpython'] = None
        self.assertNotIn('LD_LIBRARY_PATH', native.python_env(self.profile))

    def test_fixture_sites_reach_the_actual_child_environment(self):
        root = self.attempt()
        c = native.Context(root, 'preflight')
        self.assertEqual(c.ENV['PYTHONPATH'], native.python_env(self.profile)['PYTHONPATH'])
        self.assertEqual(c.ENV['PYTHONPATH'].split(os.pathsep)[-1], self.profile['python']['fixture_sites'][0])
        observed = native.fixture_import_probe(self.profile)
        self.assertEqual({row['module']: row['origin'] for row in observed['modules']},
                         {name: pin['origin'] for name, pin in self.profile['python']['fixture_imports'].items()})

    def test_fixture_origin_shadow_and_dependency_tamper_are_rejected(self):
        shadow = self.base / 'shadow'
        shadow.mkdir()
        (shadow / 'pyte.py').write_text('class Screen: pass\nclass Stream: pass\n')
        self.profile['python']['fixture_sites'].insert(0, str(shadow))
        with self.assertRaises(subprocess.CalledProcessError):
            native.fixture_import_probe(self.profile)
        self.profile['python']['fixture_sites'].pop(0)
        path = Path(self.profile['python']['fixture_imports']['wcwidth']['origin'])
        path.write_text('def wcwidth(value): return 2\n')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            native.fixture_closure(self.profile)

    def test_fixture_closure_covers_imported_package_children(self):
        top = Path(self.profile['python']['fixture_imports']['pyte']['origin'])
        child = top.with_name('streams.py')
        child.write_text('fixture = 1\n')
        first = native.fixture_closure(self.profile)
        self.assertIn(str(child), first)
        child.write_text('fixture = 2\n')
        self.assertNotEqual(first, native.fixture_closure(self.profile))

    def test_missing_libcap_becomes_available_only_with_scoped_profile_path(self):
        empty = self.base / 'empty-pkg-config'
        empty.mkdir()
        isolated = native.python_env(self.profile) | {'PKG_CONFIG_LIBDIR': str(empty)}
        original = os.environ.get('PKG_CONFIG_PATH')
        with patch.object(native, 'python_env', return_value=isolated):
            with self.assertRaises(subprocess.CalledProcessError):
                native.pkg_config_probe(self.profile)
            self.profile['native_build']['pkg_config'] = self.libcap_fixture()
            result = native.pkg_config_probe(self.profile)
        self.assertEqual(result['version'], '2.66')
        self.assertIn('-lcap', result['libs'])
        self.assertEqual(set(result['inputs']), set(self.profile['native_build']['pkg_config']['files']))
        self.assertEqual(os.environ.get('PKG_CONFIG_PATH'), original)
        self.plan['targets']['x86_64']['profile_sha256'] = protocol.digest(self.profile)
        c = native.Context(self.attempt(), 'frontend')
        self.assertEqual(c.ENV['PKG_CONFIG_PATH'], self.profile['native_build']['pkg_config']['path'])
        self.assertNotIn('PKG_CONFIG_PATH', native.python_env(self.profile))

    def test_libcap_required_inputs_and_hashes_are_checked(self):
        self.profile['native_build']['pkg_config'] = self.libcap_fixture()
        config = self.profile['native_build']['pkg_config']
        library = str(self.base / 'libcap/lib/libcap.so')
        expected = config['files'].pop(library)
        with self.assertRaisesRegex(ValueError, 'Pin the resolved'):
            native.pkg_config_probe(self.profile)
        config['files'][library] = expected
        (self.base / 'libcap/include/sys/capability.h').write_text('changed header\n')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            native.pkg_config_probe(self.profile)

    def test_libcap_frozen_inventory_detects_same_bytes_symlink_retarget(self):
        self.profile['native_build']['pkg_config'] = self.libcap_fixture()
        first = native.pkg_config_probe(self.profile)
        library = self.base / 'libcap/lib/libcap.so'
        replacement = library.with_name('libcap.so.other')
        replacement.write_bytes(library.read_bytes())
        library.unlink()
        library.symlink_to(replacement.name)
        second = native.pkg_config_probe(self.profile)
        self.assertEqual(first['inputs'][str(library)]['sha256'], second['inputs'][str(library)]['sha256'])
        self.assertNotEqual(first, second)

    def test_rehearsal_preflight_exercises_imports_and_real_fixture_setup(self):
        root = self.attempt()
        calls = []
        def run(c, name, argv, **kwargs):
            calls.append((name, c.ENV['PYTHONPATH']))
            return ''
        with patch.object(native, 'verify', return_value={'rehearsal_failure': None}), \
             patch.object(native.Context, 'run', run):
            report = native.stage(root, 'preflight')
        self.assertTrue(report['rehearsal'])
        self.assertEqual([name for name, _ in calls],
                         ['import-preflight', 'actual-fixture-setup', 'command-fixture-selftest'])
        self.assertTrue(all(self.profile['python']['fixture_sites'][0] in value for _, value in calls))
        spec = protocol.load(root / 'outputs/import-preflight-input.json')
        self.assertTrue({'pyte', 'wcwidth'} <= {row['module'] for row in spec['imports']})

    def test_checked_in_cargo_adapter_handles_notice_generator_version(self):
        env = {'PATH': '/usr/bin:/bin', 'CODEX_PLUS_RELEASE_CARGO': '/usr/bin/printf',
               'CODEX_PLUS_RELEASE_RUST_VERSION': '1.99.1'}
        result = subprocess.check_output([str(native.RECIPE / 'cargo'), '+1.99.1', '%s', 'metadata'],
                                         env=env, text=True)
        self.assertEqual(result, 'metadata')
        self.assertNotIn('CODEX_PLUS_RELEASE_CARGO', os.environ)

    def notice_tools(self):
        sysroot = self.base / 'sysroot'
        copyright = sysroot / 'share/doc/rust/COPYRIGHT-library.html'
        copyright.parent.mkdir(parents=True)
        copyright.write_text('Rust library notices\n')
        for name, body in {
            'rustc': 'case "$*" in\n--version) echo "rustc 1.99.1 (fixture)";;\n'
                     f'"--print sysroot") echo "{sysroot}";;\n*) exit 9;;\nesac\n',
            'cargo': 'if [ "$1" = --version ]; then echo "cargo 1.99.1 (fixture)"; '
                     'else echo "offline fixture source unavailable" >&2; exit 17; fi\n',
        }.items():
            tool = self.base / name
            tool.write_text('#!/bin/sh\nset -eu\n' + body)
            tool.chmod(0o755)
            self.profile['toolchain'][name] = str(tool)
        self.observed['toolchain'] = copy.deepcopy(self.profile['toolchain'])
        self.plan['targets']['x86_64']['profile_sha256'] = protocol.digest(self.profile)
        return copyright

    def test_notice_generator_resolves_pinned_compiler_in_child_environment(self):
        self.notice_tools()
        c = native.Context(self.attempt(), 'frontend')
        result = subprocess.run([c.PYTHON, '-B', str(c.SOURCE / 'scripts/runtime-notices.py'),
                                 '--target', c.TARGET, '--output-dir', str(c.BASE / 'notices')],
                                env=c.ENV, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('cargo failed: offline fixture source unavailable', result.stderr)

    def test_compiler_adapter_only_strips_the_exact_pinned_version(self):
        self.notice_tools()
        c = native.Context(self.attempt(), 'frontend')
        for prefix, code in (([], 0), (['+1.99.1'], 0), (['+1.99.0'], 9)):
            with self.subTest(prefix=prefix):
                result = subprocess.run(['rustc', *prefix, '--version'], env=c.ENV,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, code)
                if code == 0:
                    self.assertEqual(result.stdout.strip(), 'rustc 1.99.1 (fixture)')

    def packaging_fixture(self):
        copyright = self.notice_tools()
        c = native.Context(self.attempt(), 'frontend')
        c.UPSTREAM.mkdir()
        for name in ('LICENSE', 'NOTICE', 'codex-rs/vendor/bubblewrap/COPYING',
                     'third_party/v8/rusty_v8_152_2_0_release_manifests.sha256'):
            path = c.UPSTREAM / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / 'LICENSE').read_bytes())
        for name in ('cli', 'code-mode-host'):
            path = c.UPSTREAM / 'codex-rs' / name / 'Cargo.toml'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'[package]\nname = "codex-{name}"\nversion = "1.0.0"\n')
        (c.UPSTREAM / 'codex-rs/Cargo.toml').write_text('[workspace]\nmembers=["cli","code-mode-host"]\n')
        (c.UPSTREAM / 'codex-rs/Cargo.lock').write_text('version=4\n' + ''.join(
            f'[[package]]\nname="codex-{name}"\nversion="1.0.0"\n' for name in ('cli', 'code-mode-host')))
        cargo = Path(self.profile['toolchain']['cargo'])
        cargo.write_text('#!' + c.PYTHON + '\n' + '''import json,sys,tomllib
from pathlib import Path
args=sys.argv[1:]
if args == ['--version']:
    print('cargo 1.99.1 (fixture)')
    raise SystemExit(0)
assert '--offline' in args and '--locked' in args
manifest=Path(args[args.index('--manifest-path')+1])
names={'codex-plus-runtime': manifest} if manifest.parent.name == 'rust-runtime' else {
    'codex-cli': manifest.parent/'cli/Cargo.toml',
    'codex-code-mode-host': manifest.parent/'code-mode-host/Cargo.toml'}
packages=[dict(id=name,name=name,version=tomllib.loads(path.read_text())['package']['version'],
    source=None,license='MIT',manifest_path=str(path)) for name,path in names.items()]
if args[0] == 'metadata':
    print(json.dumps(dict(packages=packages,resolve=dict(root=packages[0]['id'],
        nodes=[dict(id=p['id'],deps=[]) for p in packages]))))
else:
    assert args[0] == 'tree'
    print('\\n'.join(p['name']+' v'+p['version']+'|' for p in packages))
''')
        recipe = native.module(native.RECIPE / 'packaging-preflight.py')
        notices = native.module(ROOT / 'scripts/runtime-notices.py')
        packager = native.module(ROOT / 'scripts/package-release.py')
        cache = Path(c.profile['native_build']['license_cache'])
        cache.mkdir()
        license_file = cache / 'fixture-license'
        license_file.write_text('fixture cached license')
        packager.LICENSE_DOWNLOADS = {'fixture-license': ('fixture-url', native.sha(license_file))}
        supplement = self.base / 'supplement.json'
        supplement.write_text('{}')
        notices.LICENSE_SOURCES = supplement
        return recipe, c, notices, packager, cache, copyright

    def test_packaging_preflight_exercises_both_product_notice_graphs(self):
        recipe, c, notices, packager, cache, _ = self.packaging_fixture()
        output = c.BASE / 'evidence/packaging-preflight'
        with patch.object(recipe, 'module', side_effect=[notices, packager]), \
             patch.dict(os.environ, c.ENV, clear=True):
            report = recipe.check(c.SOURCE, c.UPSTREAM, c.TARGET, cache, output)
        self.assertEqual(report['packages'], {'helper': 1, 'upstream': 2})
        self.assertEqual(report['outcome'], 'passed')
        self.assertEqual(set(report['compiler_versions']), {'rustc', 'cargo'})
        self.assertEqual(len(report['product_scripts']), 2)
        self.assertTrue((output / 'preflight.json').is_file())

    def test_packaging_preflight_retains_0163_v8_manifest(self):
        recipe, c, notices, packager, cache, _ = self.packaging_fixture()
        directory = c.UPSTREAM / 'third_party/v8'
        current = directory / 'rusty_v8_152_2_0_release_manifests.sha256'
        previous = directory / 'rusty_v8_150_4_0_release_manifests.sha256'
        current.rename(previous)
        with patch.object(recipe, 'module', side_effect=[notices, packager]), \
             patch.object(packager, 'UPSTREAM_VERSION', '0.163.0'), \
             patch.dict(os.environ, c.ENV, clear=True):
            report = recipe.check(c.SOURCE, c.UPSTREAM, c.TARGET, cache, c.BASE / 'notices')
        self.assertIn(str(previous), report['source_license_inputs'])
        self.assertNotIn(str(current), report['source_license_inputs'])

    def test_packaging_preflight_refuses_missing_compiler_and_license_inputs(self):
        for missing in ('compiler', 'compiler-version', 'cargo-version', 'copyright',
                        'cache', 'upstream-license', 'offline-source'):
            with self.subTest(missing=missing):
                # Each case owns a fresh fixture, including the attempt's socket root.
                fixture = NativeTests()
                fixture.setUp()
                try:
                    recipe, c, notices, packager, cache, copyright = fixture.packaging_fixture()
                    if missing == 'compiler':
                        c.ENV['PATH'] = '/usr/bin:/bin'
                    elif missing in ('compiler-version', 'cargo-version'):
                        tool = 'rustc' if missing == 'compiler-version' else 'cargo'
                        Path(c.profile['toolchain'][tool]).write_text(
                            '#!/bin/sh\necho "' + tool + ' 1.99.0 (wrong pin)"\n')
                    elif missing == 'copyright':
                        copyright.unlink()
                    elif missing == 'cache':
                        (cache / 'fixture-license').unlink()
                    elif missing == 'upstream-license':
                        (c.UPSTREAM / 'LICENSE').unlink()
                    else:
                        Path(c.profile['toolchain']['cargo']).write_text(
                            '#!/bin/sh\nif [ "$1" = --version ]; then echo "cargo 1.99.1 (fixture)"; '
                            'else echo "offline source unavailable" >&2; exit 17; fi\n')
                    with patch.object(recipe, 'module', side_effect=[notices, packager]), \
                         patch.dict(os.environ, c.ENV, clear=True), self.assertRaises((OSError, ValueError)):
                        recipe.check(c.SOURCE, c.UPSTREAM, c.TARGET, cache, c.BASE / 'notices')
                    self.assertFalse((c.BASE / 'notices/preflight.json').exists())
                finally:
                    fixture.doCleanups()

    def test_supplemental_preflight_checks_only_generator_download_inputs(self):
        recipe = native.module(native.RECIPE / 'packaging-preflight.py')
        supplements = {'fixture': {'basis': 'published-license-declaration',
            'archive': {'sha256': 'archive'},
            'source_evidence': {'Cargo.toml': {'url': 'archive-url', 'sha256': 'local-source'}},
            'files': [{'sha256': 'remote'}, {'sha256': 'local', 'local_path': 'Cargo.toml'},
                      {'sha256': 'embedded', 'text': 'embedded notice'}]}}
        self.assertEqual(list(recipe.supplemental_inputs(supplements, self.base)),
                         [(self.base / 'remote', 'remote'), (self.base / 'archive', 'archive')])

    def test_prepare_missing_input_refuses_before_any_process_launch(self):
        root = self.attempt()
        with patch.object(native, 'observe') as observe, patch.object(native, 'owned_run') as execute:
            with self.assertRaisesRegex(ValueError, 'Missing native input'):
                native.prepare(root, self.hook)
            observe.assert_not_called()
            execute.assert_not_called()
        self.assertFalse((root / 'job-proposal.json').exists())

    def test_prepare_freezes_normal_proposal_without_launch(self):
        build = self.profile['native_build']
        for name in ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache', 'license_cache'):
            Path(build[name]).mkdir()
        Path(build['official_binary']).write_text('official')
        build['official_sha256'] = native.sha(build['official_binary'])
        build['pkg_config'] = self.libcap_fixture()
        self.plan['targets']['x86_64']['profile_sha256'] = protocol.digest(self.profile)
        root = self.attempt()
        closure = {str(ROOT / 'scripts/release_native.py'): protocol.file_record(ROOT / 'scripts/release_native.py')}
        with patch.object(native, 'observe', return_value=(self.observed, {'version': 'test'})), \
             patch.object(native, 'checkout_identity', return_value={'head': 'fixture'}), \
             patch.object(native, 'input_closure', return_value=closure), \
             patch.object(native, 'module') as ownership, \
             patch.object(native, 'owned_run') as execute:
            ownership.return_value.capture_baseline.return_value = {'boot_id': 'fixture', 'processes': {}}
            proposal = native.prepare(root, self.hook, rehearsal_failure='frontend')
        execute.assert_not_called()
        self.assertIn('{execution}', proposal['argv'])
        self.assertEqual(proposal['cwd'], str(ROOT))
        self.assertNotIn('CODEX_PLUS_RELEASE_ATTEMPT', proposal['env'])
        self.assertIn('cleanup', proposal)
        self.assertTrue((root / 'ownership-baseline.json').is_file())
        prepared = protocol.load(root / 'native-preparation.json')
        self.assertEqual(set(prepared['pkg_config']['inputs']), set(build['pkg_config']['files']))
        pkg_snapshots = [row for row in proposal['inputs'] if row['path'].startswith('pkg-config/')]
        self.assertEqual({row['source'] for row in pkg_snapshots},
                         {record['resolved'] for record in prepared['pkg_config']['inputs'].values()})
        self.assertEqual(protocol.load(root / 'job-proposal.json'), proposal)
        self.assertEqual(protocol.load(root / 'native-preparation.json')['rehearsal_failure'], 'frontend')

    def test_each_stage_failure_has_immutable_receipt_and_stops(self):
        for failed_stage in native.STAGES:
            with self.subTest(stage=failed_stage):
                root = self.attempt()
                context = self.setup_runner(root)
                invoked = []
                def fail(argv, **kwargs):
                    stage = argv[argv.index('--stage') + 1]
                    invoked.append(stage)
                    code = self.fake_stage(argv, **kwargs)
                    return 17 if stage == failed_stage else code
                with patch.object(native, 'verify', return_value=context), \
                     patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
                     patch.object(native, 'owned_run', side_effect=fail):
                    with self.assertRaisesRegex(ValueError, 'preserve logs'):
                        native.run(root, self.base / 'execution')
                    failed = root / 'receipts' / (failed_stage + '.json')
                    before = failed.read_bytes()
                    receipt = protocol.load(failed)
                    self.assertEqual(receipt['input']['execution']['exit_code'], 17)
                    self.assertEqual(receipt['input']['execution']['outcome'], 'failed')
                    self.assertEqual(invoked, list(native.STAGES[:native.STAGES.index(failed_stage) + 1]))
                    with self.assertRaisesRegex(ValueError, 'immutable'):
                        native.run(root, self.base / 'execution')
                    self.assertEqual(before, failed.read_bytes())

    def test_package_failure_records_artifacts_installed_from_private_build(self):
        root = self.attempt()
        context = self.setup_runner(root)
        c = native.Context(root, 'frontend')
        built = c.CARGO_TARGET / c.TARGET / 'release/codex'
        built.parent.mkdir(parents=True)
        built.write_bytes(core_tests.packaging.elf(62))
        built.chmod(0o700)
        error = b'packaged-footer-rows failed (1); original footer assertion\n'
        invoked = []

        def fail_package(argv, *, stdout, **kwargs):
            stage = argv[argv.index('--stage') + 1]
            invoked.append(stage)
            if stage == 'frontend':
                with patch.object(c, 'run', return_value=''):
                    c.install_native('codex')
            if stage == 'package':
                packaged = c.PACKAGE / 'bin/codex'
                packaged.parent.mkdir(parents=True)
                shutil.copy2(c.NATIVE / 'codex', packaged)
                packaged.chmod(0o755)
                self.assertEqual(native.sha(packaged), native.sha(built))
                self.assertEqual(packaged.stat().st_size, built.stat().st_size)
                stdout.write(error)
                return 1
            return self.fake_stage(argv, stdout=stdout, **kwargs)

        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=fail_package):
            with self.assertRaisesRegex(ValueError, 'Stage package failed; preserve logs'):
                native.run(root, self.base / 'execution')
            receipt_path = root / 'receipts/package.json'
            original = receipt_path.read_bytes()
            receipt = protocol.load(receipt_path)
            self.assertEqual(receipt['input']['execution']['outcome'], 'failed')
            self.assertEqual(receipt['input']['execution']['exit_code'], 1)
            self.assertEqual((root / 'logs/package.log').read_bytes(), error)
            self.assertEqual(receipt['outputs']['logs/package.log'],
                             protocol.file_record(root / 'logs/package.log'))
            self.assertEqual(receipt['input']['executed'][str(c.PACKAGE / 'bin/codex')]['mode'], '0755')
            self.assertEqual(protocol.load(root / 'receipts/frontend.json')['outputs']['native/codex']['mode'], '0755')
            self.assertEqual(c.NATIVE.stat().st_mode & 0o777, 0o700)
            self.assertEqual(protocol.status(root)['refused'], ['package'])
            with self.assertRaisesRegex(ValueError, 'Missing/failed'):
                protocol.approve_package(root, source_root=ROOT)
            with self.assertRaisesRegex(ValueError, 'immutable'):
                native.run(root, self.base / 'execution')
            self.assertEqual(receipt_path.read_bytes(), original)
            self.assertEqual(invoked, list(native.STAGES[:native.STAGES.index('package') + 1]))

    def test_unsealed_runner_skips_passed_receipts_and_rejects_tamper(self):
        # The mocked cleanup never seals the socket; this does not qualify post-seal managed resume.
        root = self.attempt()
        context = self.setup_runner(root)
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=self.fake_stage) as execute:
            state = native.run(root, self.base / 'execution')
            self.assertFalse(state['refused'])
            self.assertEqual(execute.call_count, len(native.STAGES))
            snapshot = {path: path.read_bytes() for path in (root / 'receipts').glob('*.json')}
            native.run(root, self.base / 'execution')
            self.assertEqual(execute.call_count, len(native.STAGES))
            self.assertEqual(snapshot, {path: path.read_bytes() for path in snapshot})
            target = root / 'outputs/preflight.json'
            target.chmod(0o600)
            target.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed'):
                native.run(root, self.base / 'execution')
            self.assertEqual(execute.call_count, len(native.STAGES))

    def test_interrupted_stage_is_unknown_and_not_replayed(self):
        root = self.attempt()
        context = self.setup_runner(root)
        protocol.write_once(root / 'native-starts/preflight.json', {'job_id': 'old-job'})
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run') as execute:
            with self.assertRaisesRegex(ValueError, 'unknown outcome'):
                native.run(root, self.base / 'execution')
            execute.assert_not_called()

    def test_missing_stage_report_cannot_pass_on_zero_exit(self):
        root = self.attempt()
        context = self.setup_runner(root)
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', return_value=0):
            with self.assertRaisesRegex(ValueError, 'failed'):
                native.run(root, self.base / 'execution')
        self.assertEqual(protocol.load(root / 'receipts/preflight.json')['input']['execution']['outcome'], 'failed')

    def test_cleanup_failure_remains_unknown_and_preserves_report(self):
        root = self.attempt()
        context = self.setup_runner(root)
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=self.fake_stage), \
             patch.object(native, 'cleanup', side_effect=RuntimeError('lost cleanup')):
            with self.assertRaisesRegex(ValueError, 'preserve logs'):
                native.run(root, self.base / 'execution')
        row = protocol.load(root / 'receipts/preflight.json')
        self.assertEqual(row['cleanup']['outcome'], 'unknown')
        self.assertEqual(row['cleanup']['unknown'], 1)
        self.assertTrue((root / row['cleanup']['report']).exists())

    def test_blocked_cleanup_retains_raw_owned_counts_without_forging_clean(self):
        root = self.attempt()
        context = self.setup_runner(root)
        def blocked(root, report):
            protocol.write_once(report, {'outcome': 'blocked', 'owned_remaining': [{'pid': 4}], 'unknown': []})
            raise ValueError('live owned process')
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=self.fake_stage), \
             patch.object(native, 'cleanup', side_effect=blocked):
            with self.assertRaisesRegex(ValueError, 'preserve logs'):
                native.run(root, self.base / 'execution')
        row = protocol.load(root / 'receipts/preflight.json')
        self.assertEqual(row['cleanup']['owned_remaining'], 1)
        self.assertEqual(row['cleanup']['outcome'], 'unknown')
        self.assertEqual(protocol.load(root / 'outputs/preflight-cleanup.json')['outcome'], 'blocked')
        self.assertEqual(protocol.status(root)['refused'], ['preflight'])

    def test_supervisor_unknown_outcome_is_never_replayed(self):
        root = self.attempt()
        context = self.setup_runner(root)
        with patch.object(native, 'verify', return_value=context), \
             patch.object(native, 'execution_identity', return_value=('fixture-job', 'c' * 64)), \
             patch.object(native, 'owned_run', side_effect=subprocess.TimeoutExpired(['stage'], 10)) as execute:
            with self.assertRaisesRegex(ValueError, 'unknown'):
                native.run(root, self.base / 'execution')
            row = protocol.load(root / 'receipts/preflight.json')
            self.assertIsNone(row['input']['execution']['exit_code'])
            self.assertEqual(row['input']['execution']['outcome'], 'unknown')
            with self.assertRaisesRegex(ValueError, 'immutable'):
                native.run(root, self.base / 'execution')
            self.assertEqual(execute.call_count, 1)

    def test_command_failure_preserves_actual_exit_and_logs(self):
        root = self.attempt()
        (root / 'logs').mkdir()
        c = native.Context(root, 'preflight')
        with self.assertRaisesRegex(ValueError, 'failed \\(9\\)'):
            c.run('fails', [sys.executable, '-c', 'print("failure detail");raise SystemExit(9)'])
        result = protocol.load(root / 'evidence/commands/fails.json')
        self.assertEqual(result['exit_code'], 9)
        self.assertEqual(result['executable'], str(Path(sys.executable).resolve()))
        self.assertIn('failure detail', (root / 'logs/fails.log').read_text())

    def test_direct_run_requires_exact_executor_package_and_command(self):
        root = self.attempt()
        helper = self.base / 'executor/bin/codex-plus-runtime'
        helper.parent.mkdir(parents=True)
        helper.write_text('installed helper')
        profile = dict(self.profile, executor_package_root=str(helper.parent.parent))
        directory = self.base / 'execution-id'
        directory.mkdir()
        state = {'id': directory.name, 'state': 'running', 'package': profile['executor_package_root'],
                 'runtime': {'sha256': native.sha(helper)},
                 'workload': native.process_identity(os.getpid()),
                 'manifest': {'command': {'spec': {'argv': [sys.executable, '-B', native.__file__,
                     'run', '--attempt', str(root), '--execution', str(directory)]}}}}
        path = directory / 'state.json'
        path.write_text(json.dumps(state))
        self.assertEqual(native.execution_identity(directory, root, profile)[0], directory.name)
        state['workload']['start_ticks'] += 1
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'does not own this process'):
            native.execution_identity(directory, root, profile)
        state['workload'] = native.process_identity(os.getpid())
        state['package'] = '/another/package'
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            native.execution_identity(directory, root, profile)

    def test_alternate_index_bytes_and_mtime_survive_verify(self):
        source = self.base / 'repository'
        source.mkdir()
        env = os.environ | {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        def git(*args):
            return subprocess.check_output(['/usr/bin/git', '-C', str(source), *args], env=env,
                                           stderr=subprocess.STDOUT)
        git('init')
        (source / 'file').write_text('source\n')
        git('add', 'file')
        git('-c', 'user.email=fixture@example.test', '-c', 'user.name=Fixture', 'commit', '-m', 'fixture')
        index = source / '.git/index'
        alternate = self.base / 'sealed-alternate.index'
        shutil.copy2(index, alternate)
        stat = (source / 'file').stat()
        os.utime(source / 'file', ns=(stat.st_atime_ns, stat.st_mtime_ns - 1000000))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (index, alternate)}
        with patch.dict(os.environ, {'GIT_INDEX_FILE': str(alternate)}):
            first = native.checkout_identity(source)
            second = native.checkout_identity(source)
        self.assertEqual(first, second)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})

    def test_owned_supervisor_actual_exit_and_journal(self):
        helper = Path('/home/liupan/Aerius/codex-plus/.build/codex-plus-runtime')
        if not helper.is_file():
            self.skipTest('No local built supervisor; composed proof remains required')
        root = self.attempt()
        with (root / 'owned.log').open('wb') as stream:
            code = native.owned_run([sys.executable, '-c', 'print("owned child");raise SystemExit(7)'],
                helper=helper, python=sys.executable, cwd=root, env=native.python_env(self.profile),
                stdout=stream, timeout=10)
        self.assertEqual(code, 7)
        self.assertIn('owned child', (root / 'owned.log').read_text())
        rows = [json.loads(line) for line in (root / 'process-journal.jsonl').read_text().splitlines()]
        self.assertEqual([row['event'] for row in rows], ['started', 'finished'])
        self.assertEqual(rows[0]['identity'], rows[1]['identity'])
        self.assertEqual(rows[1]['exit_code'], 7)

    def test_recipe_is_version_data_and_keeps_bwrap_before_frontend(self):
        recipe = native.module(native.RECIPE / 'build.py')
        source = (native.RECIPE / 'build.py').read_text()
        self.assertLess(source.index("c.run('bwrap-build'"), source.index("c.run('frontend-build'"))
        for text in ('release047', 'release030', 'd25cc24', '/data/liupan'):
            self.assertNotIn(text, '\n'.join(path.read_text() for path in native.RECIPE.glob('*.py')))
        root = self.attempt()
        old = native.Context(root, 'frontend')
        next_plan = copy.deepcopy(self.plan)
        prefix, patch_version = self.plan['release'].rsplit('.', 1)
        next_plan['release'] = f'{prefix}.{int(patch_version) + 1}'
        next_plan['plus_source_commit'] = 'e' * 40
        with patch.object(native.protocol, 'read_attempt', return_value=(protocol.load(root / 'attempt.json'), next_plan, self.profile)):
            new = native.Context(root, 'frontend')
        self.assertNotEqual(old.PACKAGE, new.PACKAGE)
        self.assertEqual(old.CARGO, new.CARGO)
        self.assertIn('worker_hud', recipe.FILTERS)
        self.assertIn('execution_hud', recipe.FILTERS)

    def test_cargo_artifact_parser_preserves_records_and_ignores_non_artifact_output(self):
        recipe = native.module(native.RECIPE / 'build.py')
        artifacts = [{'reason': 'compiler-artifact', 'fresh': True, 'target': {'name': 'dependency'}},
                     {'reason': 'compiler-artifact', 'fresh': False, 'target': {'name': 'codex'}}]
        output = '\n'.join(['   Compiling codex', '{incomplete diagnostic', 'null', '[]',
                            json.dumps({'reason': 'compiler-message', 'message': {'rendered': 'warning'}}),
                            *(json.dumps(row) for row in artifacts),
                            json.dumps({'reason': 'build-finished', 'success': True})])
        self.assertEqual(recipe.compiler_artifacts(output), artifacts)

    def test_cargo_artifact_parser_refuses_unknown_freshness(self):
        recipe = native.module(native.RECIPE / 'build.py')
        for flag in (None, 0, 1, 'true'):
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, 'freshness'):
                recipe.compiler_artifacts(json.dumps({'reason': 'compiler-artifact', 'fresh': flag}))
        with self.assertRaisesRegex(ValueError, 'freshness'):
            recipe.compiler_artifacts(json.dumps({'reason': 'compiler-artifact'}))

    def test_cargo_artifact_parser_refuses_missing_evidence(self):
        recipe = native.module(native.RECIPE / 'build.py')
        for output in ('', 'Finished release profile', json.dumps({'reason': 'build-finished', 'success': True})):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'Missing Cargo'):
                recipe.compiler_artifacts(output)

    def test_frontend_composition_uses_profile_paths_and_real_bwrap_dependency(self):
        cases = [([True, True], [True, True, True], {'bwrap-build': {'fresh': 2, 'compiled': 0},
                                                  'frontend-build': {'fresh': 3, 'compiled': 0}}),
                 ([False, False], [False, False, False], {'bwrap-build': {'fresh': 0, 'compiled': 2},
                                                         'frontend-build': {'fresh': 0, 'compiled': 3}}),
                 ([True, False], [False, True, True], {'bwrap-build': {'fresh': 1, 'compiled': 1},
                                                      'frontend-build': {'fresh': 2, 'compiled': 1}})]
        for bwrap_flags, frontend_flags, expected in cases:
            with self.subTest(expected=expected):
                root = self.attempt()
                (root / 'native').mkdir()
                (root / 'logs').mkdir()
                c = native.Context(root, 'frontend')
                calls, installed = [], []
                def execute(name, argv, **kwargs):
                    calls.append((name, list(map(str, argv)), dict(c.ENV)))
                    if name == 'v8-resolve':
                        return json.dumps({'RUSTY_V8_ARCHIVE': str(c.V8_CACHE / 'archive'),
                                           'RUSTY_V8_SRC_BINDING_PATH': str(c.V8_CACHE / 'binding')})
                    if name in ('bwrap-build', 'frontend-build'):
                        flags = bwrap_flags if name == 'bwrap-build' else frontend_flags
                        path = root / 'logs' / (name + '.log')
                        path.write_text('Compiling\n' + '\n'.join(json.dumps({'reason': 'compiler-artifact',
                            'fresh': flag}) for flag in flags) + '\n')
                        return path.read_text()
                    return ''
                def install(name):
                    installed.append(name)
                    (c.NATIVE / name).write_text('native artifact ' + name)
                c.run = execute
                c.install_native = install
                c.cargo_timing = lambda name: None
                c.digest = lambda path: 'a' * 64
                native.module(native.RECIPE / 'build.py').frontend(c)
                names = [name for name, _, _ in calls]
                self.assertLess(names.index('bwrap-build'), names.index('frontend-build'))
                self.assertLess(names.index('patch-apply'), names.index('packaging-preflight'))
                self.assertLess(names.index('packaging-preflight'), names.index('bwrap-build'))
                preflight = next(row for row in calls if row[0] == 'packaging-preflight')
                self.assertEqual(preflight[2], calls[0][2])
                self.assertIn(str(c.BASE / 'evidence/packaging-preflight'), preflight[1])
                self.assertEqual(calls[0][1][-1], str(root / 'upstream'))
                for name in ('bwrap-build', 'frontend-build'):
                    argv = next(row[1] for row in calls if row[0] == name)
                    self.assertIn('--message-format=json', argv)
                    self.assertEqual(argv[argv.index('--jobs') + 1], '3')
                    self.assertEqual(argv[argv.index('--target') + 1], c.TARGET)
                    self.assertTrue({'--locked', '--offline', '--release', '--timings'} <= set(argv))
                frontend = next(row for row in calls if row[0] == 'frontend-build')
                self.assertIn(str(c.CARGO), frontend[1])
                self.assertEqual(frontend[2]['CODEX_BWRAP_SHA256'], 'a' * 64)
                self.assertTrue(frontend[2]['RUSTY_V8_ARCHIVE'].startswith(str(c.V8_CACHE)))
                self.assertEqual(installed, ['bwrap', 'codex', 'codex-code-mode-host'])
                self.assertEqual(c.REPORT['cargo_artifact_counts'], expected)
                self.assertEqual(c.REPORT['native_binary_sha256'],
                                 {name: 'a' * 64 for name in installed})
                self.assertNotIn('fresh_frontend', c.REPORT)
                self.assertNotIn('frontend_rebuilt', c.REPORT)


class UpstreamQualificationTests(unittest.TestCase):
    OLD = {'version': '0.163.0', 'tag': 'rust-v0.163.0',
           'commit': '83086188ae0614bd882818ff1aa23f3194962012'}
    NEW = {'version': '0.165.0', 'tag': 'rust-v0.165.0',
           'commit': '360319d8dee62d87e2d59fc0c21805d79519cfed'}

    def test_reviewed_0165_family_reaches_compiler_validation(self):
        plan = {'upstream': self.NEW, 'previous_release': {'upstream': self.OLD},
                'compat': {'recipe_revision': 2}}
        with self.assertRaisesRegex(ValueError, 'Compiler recipe needs qualification'):
            native.build_inputs(plan, {'toolchain': {'version': '1.99.0'}})

    def test_changed_commit_tag_or_recipe_cannot_select_0165(self):
        plan = {'upstream': self.NEW, 'previous_release': {'upstream': self.OLD},
                'compat': {'recipe_revision': 2}}
        for field, value in (('tag', 'rust-v0.165.0-alpha.1'), ('commit', '0' * 40)):
            changed = copy.deepcopy(plan)
            changed['upstream'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Upstream recipe'):
                native.build_inputs(changed, {})
        for revision in (1, 3):
            changed = copy.deepcopy(plan)
            changed['compat']['recipe_revision'] = revision
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, 'Upstream recipe'):
                native.build_inputs(changed, {})

    def test_0165_filters_extend_preserved_0163_inventory(self):
        recipe = native.module(native.RECIPE / 'build.py')
        old = recipe.filters({'upstream': self.OLD})
        new = recipe.filters({'upstream': self.NEW})
        self.assertEqual(old, recipe.FILTERS)
        self.assertEqual(len(new), 19)
        self.assertTrue(set(old) < set(new))
        self.assertTrue({'plus_goal', 'ultracode', 'shell_policy', 'command_display',
                         'compact_display', 'daemon_menu', 'native_history',
                         'active_turn_steer', 'chatwidget::tests::exec_flow'} <= set(new))

    def test_0165_package_support_does_not_qualify_production_recipe(self):
        plan = {'upstream': {'version': '0.165.0'}}
        with self.assertRaisesRegex(ValueError, 'Upstream recipe needs qualification'):
            native.build_inputs(plan, {})

    def test_qualified_0163_still_checks_native_prerequisites(self):
        plan = {'upstream': self.OLD, 'compat': {'recipe_revision': 1}}
        with self.assertRaisesRegex(ValueError, 'Compiler recipe needs qualification'):
            native.build_inputs(plan, {'toolchain': {'version': '1.99.0'}})


if __name__ == '__main__':
    unittest.main()
