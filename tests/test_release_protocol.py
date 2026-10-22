"""Reject incomplete or changed native evidence without launching release work."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

import test_packaging as packaging


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_protocol', ROOT / 'scripts/release_protocol.py')
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


class ReleaseProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cp-protocol-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.profile = {'schema_version': 1, 'host_id': 'fixture-host', 'hostname': 'fixture',
            'architecture': 'x86_64', 'triple': protocol.TARGETS['x86_64'],
            'build_root': str(self.base / 'build'), 'short_root': str(self.base / 's'),
            'tooling_root': str(ROOT), 'source_root': str(ROOT),
            'toolchain': {'rustc': '/fixture/bin/rustc', 'cargo': '/fixture/bin/cargo', 'version': '1.99.1'},
            'python': {'path': '/fixture/bin/python', 'version': '3.13.7', 'libpython': None},
            'executor_package_root': '/fixture/executor'}
        previous = protocol.load(ROOT / 'docs/releases/0.163.0-plus.0.4.6/handoff.json')
        gates = {gate: {'checks': [gate + '-check']} for gate in protocol.GATES}
        gates['native-tests'] = {'suites': {'hud': {'passed': 1, 'allowed_ignored': {}}}}
        self.plan = {'schema_version': 1, 'release': packaging.package_release.RELEASE,
            'plus_version': packaging.package_release.PLUS_VERSION,
            'upstream': previous['upstream'], 'plus_source_commit': 'a' * 40,
            'tooling_commit': 'b' * 40,
            'patch': {'path': packaging.package_release.PATCH,
                      'sha256': protocol.file_record(ROOT / packaging.package_release.PATCH)['sha256']},
            'previous_release': {'tag': previous['release'],
                'handoff_sha256': protocol.file_record(ROOT / 'docs/releases/0.163.0-plus.0.4.6/handoff.json')['sha256'],
                'targets': {arch: {key: value for key, value in row.items() if key != 'triple'}
                            for arch, row in previous['targets'].items()}},
            'targets': {arch: {'triple': triple, 'profile_sha256': protocol.digest(self.profile), 'build_glibc': '2.39'}
                        for arch, triple in protocol.TARGETS.items()},
            'gates': {arch: copy.deepcopy(gates) for arch in protocol.TARGETS},
            'compat': {'ap_version': '0.7.35', 'fixture_ssh_destinations': [], 'recipe_revision': 1,
                       'previous_source_commit': previous['plus_commit'],
                       'stock_binary_sha256': {arch: 'd' * 64 for arch in protocol.TARGETS},
                       'fixture_files': {'scripts/native-results.py': protocol.file_record(ROOT / 'scripts/native-results.py')['sha256']}},
            'rollout': {'daemon_restart': False, 'quiet_checkpoint': True, 'executions': 'wait',
                        'session_restart': 'required', 'agents_md': None, 'retain': [previous['release']],
                        'notes': 'Fixture only'},
            'retention_floor': previous['release'], 'note': 'docs/releases/fixture.md'}
        self.observed = {name: copy.deepcopy(self.profile[name]) for name in
                         ('hostname', 'architecture', 'toolchain', 'python', 'executor_package_root')}
        self.observed.update(plus_source_commit=self.plan['plus_source_commit'], tooling_commit=self.plan['tooling_commit'])

    def attempt(self, **kwargs):
        return protocol.init_attempt(self.plan, self.profile, ROOT, observed=self.observed, **kwargs)

    def put(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return name

    def receipt(self, root, stage, report, extra=(), *, outcome='passed', exit_code=0, cleanup=None,
                replay_profile=None, raw_cleanup=None):
        raw = self.put(root, f'outputs/{stage}/raw.log', 'recorded native output\n')
        cleanup = cleanup or {'outcome': 'clean', 'owned_remaining': 0, 'unknown': 0,
                              'report': f'outputs/{stage}/cleanup.json'}
        self.put(root, cleanup['report'], raw_cleanup if raw_cleanup is not None else cleanup)
        if stage != 'native-tests':
            report['checks'] = {name: {'outcome': 'passed', 'evidence': {raw: protocol.file_record(root / raw)['sha256']}}
                                for name in self.plan['gates']['x86_64'][stage]['checks']}
        report.update(architecture='x86_64', plus_source_commit=self.plan['plus_source_commit'])
        evidence = self.put(root, f'outputs/{stage}/report.json', report)
        execution = {'job_id': 'fixture-job-' + stage, 'executor_package_root': self.profile['executor_package_root'],
                     'executor_sha256': 'c' * 64, 'architecture': 'x86_64',
                     'plus_source_commit': self.plan['plus_source_commit'], 'tooling_commit': self.plan['tooling_commit'],
                     'outcome': outcome, 'exit_code': exit_code, 'argv': ['fixture', stage],
                     'cwd': str(ROOT), 'env': {}}
        if replay_profile is not None:
            execution.update(architecture=replay_profile['architecture'],
                             executor_package_root=replay_profile['executor_package_root'],
                             replay_profile_sha256=protocol.digest(replay_profile))
        return protocol.write_receipt(root, stage, inputs=['plan.json'],
            executed=[str(ROOT / 'scripts/native-results.py')],
            outputs=[raw, evidence, cleanup['report'], *extra], evidence=evidence,
            execution=execution, cleanup=cleanup, replay_profile=replay_profile)

    def qualified(self, *, rehearsal=False, skip_ap=False, native_result='ok one\n', corrupt_archive=False):
        root = self.attempt(rehearsal=rehearsal)
        fixture = packaging.PackageIntegrityTests('test_valid_package_and_declared_hash_recipe')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.manifest['frontend_patch_sha256'] = self.plan['patch']['sha256']
        fixture.seal()
        package = root / 'outputs' / fixture.package.name
        shutil.copytree(fixture.package, package)
        archive = root / 'outputs/candidate.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            def filter_member(member):
                if corrupt_archive and member.name.endswith('/bin/codex'):
                    member.mode = 0o600
                return member
            tar.add(package, arcname=package.name, filter=filter_member)
        package_files = [p.relative_to(root).as_posix() for p in package.rglob('*') if p.is_file()]
        self.receipt(root, 'package', {'package_dir': package.relative_to(root).as_posix(),
                                     'archive': archive.relative_to(root).as_posix()},
                     [archive.relative_to(root).as_posix(), *package_files])
        listed = self.put(root, 'outputs/native-tests/list', 'one: test\n')
        stdout = self.put(root, 'outputs/native-tests/stdout',
                          'test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0s\n')
        logfile = self.put(root, 'outputs/native-tests/logfile', native_result)
        self.receipt(root, 'native-tests', {'suites': {'hud': {'listed': listed, 'stdout': stdout,
                     'logfile': logfile, 'exit_code': 0}}}, [listed, stdout, logfile])
        self.receipt(root, 'compat', {})
        package_sha = protocol.file_record(archive)['sha256']
        capture = self.put(root, 'outputs/captures/screen.txt', 'packaged TUI capture\n')
        manifest = self.put(root, 'outputs/captures/manifest.json', {'architecture': 'x86_64',
            'package_sha256': package_sha, 'files': {capture: protocol.file_record(root / capture)}})
        manifest_sha = protocol.file_record(root / manifest)['sha256']
        self.receipt(root, 'captures', {'package_sha256': package_sha, 'capture_manifest': manifest,
                                     'capture_manifest_sha256': manifest_sha}, [capture, manifest])
        if not skip_ap:
            self.receipt(root, 'ap', {'ap_version': '0.7.35', 'package_sha256': package_sha,
                                    'capture_manifest_sha256': manifest_sha})
        return root

    def test_plan_checks_source_cargo_patch_and_unknown_fields(self):
        protocol.validate_plan(self.plan, ROOT)
        for mutate in (lambda p: p.update(plus_version='99.0.0'),
                       lambda p: p.update(unknown='ignored'),
                       lambda p: p['patch'].update(sha256='0' * 64),
                       lambda p: p['gates']['aarch64'].pop('captures'),
                       lambda p: p['previous_release']['targets'].pop('aarch64')):
            plan = copy.deepcopy(self.plan)
            mutate(plan)
            with self.assertRaises(ValueError):
                protocol.validate_plan(plan, ROOT)

    def test_next_version_is_data_and_product_constants_only(self):
        source = self.base / 'next-source'
        for name in ('scripts/package-release.py', 'rust-runtime/Cargo.toml', 'rust-runtime/Cargo.lock', self.plan['patch']['path']):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((ROOT / name).read_text())
        next_version = '0.4.8'
        for name in ('scripts/package-release.py', 'rust-runtime/Cargo.toml', 'rust-runtime/Cargo.lock'):
            path = source / name
            path.write_text(path.read_text().replace(self.plan['plus_version'], next_version))
        plan = copy.deepcopy(self.plan)
        plan.update(plus_version=next_version, release=plan['upstream']['version'] + '-plus.' + next_version)
        protocol.validate_plan(plan, source)
        (source / 'rust-runtime/Cargo.lock').write_text((ROOT / 'rust-runtime/Cargo.lock').read_text())
        with self.assertRaisesRegex(ValueError, 'Cargo lock'):
            protocol.validate_plan(plan, source)

    def test_native_profile_requires_observed_host_runtime_and_source(self):
        for field, value in [('architecture', 'aarch64'), ('hostname', 'elsewhere'),
                             ('plus_source_commit', 'f' * 40), ('python', dict(self.profile['python'], version='wrong'))]:
            observed = copy.deepcopy(self.observed)
            observed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                protocol.init_attempt(self.plan, self.profile, ROOT, observed=observed)

    def test_attempts_are_unique_private_and_metadata_is_immutable(self):
        first, second = self.attempt(), self.attempt()
        self.assertNotEqual(first, second)
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        socket_root = Path(protocol.load(first / 'attempt.json')['socket_root'])
        self.assertEqual(len(socket_root.name), 2)
        self.assertEqual(socket_root.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            protocol.write_once(first / 'attempt.json', {})
        self.assertEqual(protocol.status(first)['missing'], list(protocol.GATES))

    def test_accept_replays_existing_package_and_native_validators(self):
        root = self.qualified()
        accepted = protocol.accept_attempt(root, source_root=ROOT)
        self.assertTrue(accepted['accepted'])
        self.assertEqual(accepted['target']['triple'], protocol.TARGETS['x86_64'])
        self.assertEqual(set(accepted['receipts']), set(protocol.GATES))
        other = copy.deepcopy(accepted)
        other['architecture'] = 'aarch64'
        other['target']['triple'] = protocol.TARGETS['aarch64']
        handoff = protocol.make_handoff(self.plan, {'x86_64': accepted, 'aarch64': other})
        self.assertEqual(set(handoff), set(protocol.load(ROOT / 'docs/releases/0.163.0-plus.0.4.7/handoff.json')))

    def test_modified_output_input_receipt_and_attempt_are_rejected(self):
        for name in ('outputs/native-tests/logfile', 'receipts/native-tests.json', 'plan.json', 'attempt.json'):
            with self.subTest(name=name):
                root = self.qualified()
                path = root / name
                path.chmod(0o600)
                if name.endswith('logfile'):
                    path.write_text('ok forged\n')
                else:
                    value = protocol.load(path)
                    value['attempt' if name == 'attempt.json' else 'tampered'] = 'changed'
                    path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    protocol.accept_attempt(root, source_root=ROOT)

    def test_missing_gate_and_rehearsal_are_not_accepted(self):
        root = self.attempt()
        with self.assertRaisesRegex(ValueError, 'Missing'):
            protocol.accept_attempt(root, source_root=ROOT)
        root = self.qualified(rehearsal=True)
        with self.assertRaisesRegex(ValueError, 'Rehearsal'):
            protocol.accept_attempt(root, source_root=ROOT)

    def test_unknown_nonzero_and_unknown_cleanup_preserve_failed_evidence(self):
        for outcome, exit_code, cleanup in [
            ('unknown', None, None), ('failed', 9, None),
            ('passed', 0, {'outcome': 'unknown', 'owned_remaining': 0, 'unknown': 1,
                           'report': 'outputs/package/cleanup.json'})]:
            root = self.attempt()
            self.receipt(root, 'package', {}, outcome=outcome, exit_code=exit_code, cleanup=cleanup)
            before = (root / 'receipts/package.json').read_bytes()
            self.assertEqual(protocol.status(root)['refused'], ['package'])
            row = protocol.load(root / 'receipts/package.json')
            with self.assertRaises(ValueError):
                protocol.write_receipt(root, 'package', inputs=['plan.json'],
                    executed=[str(ROOT / 'scripts/native-results.py')], outputs=list(row['outputs']),
                    evidence=row['evidence'], execution=row['input']['execution'], cleanup=row['cleanup'])
            self.assertEqual((root / 'receipts/package.json').read_bytes(), before)
            bundle = self.base / (root.name + '.tgz')
            record = protocol.export_attempt(root, bundle)
            imported = protocol.import_attempt(bundle, self.base / ('import-' + root.name), expected_sha256=record['sha256'])
            self.assertEqual(protocol.status(imported)['refused'], ['package'])

    def test_no_exit_zero_only_checks_wrong_source_or_wrong_arch(self):
        for mutation in ('exit-only', 'wrong-source', 'wrong-arch'):
            root = self.attempt()
            self.receipt(root, 'package', {})
            row = protocol.load(root / 'receipts/package.json')
            report = protocol.load(root / row['evidence'])
            if mutation == 'exit-only':
                report['checks']['package-check'] = {'outcome': 'passed'}
                with self.assertRaises(ValueError):
                    protocol._check_report(report, ['package-check'], row['outputs'])
            else:
                execution = copy.deepcopy(row['input']['execution'])
                execution['plus_source_commit' if mutation == 'wrong-source' else 'architecture'] = 'c' * 40 if mutation == 'wrong-source' else 'aarch64'
                with self.assertRaises(ValueError):
                    protocol.write_receipt(root, 'native-tests', inputs=['plan.json'], executed=[str(ROOT / 'scripts/native-results.py')],
                        outputs=list(row['outputs']), evidence=row['evidence'], execution=execution, cleanup=row['cleanup'])

    def test_export_import_requires_exact_transport_digest_and_preserves_receipts(self):
        root = self.qualified()
        bundle = self.base / 'export.tgz'
        record = protocol.export_attempt(root, bundle)
        with self.assertRaisesRegex(ValueError, 'Transport'):
            protocol.import_attempt(bundle, self.base / 'bad', expected_sha256='0' * 64)
        imported = protocol.import_attempt(bundle, self.base / 'imported', expected_sha256=record['sha256'])
        self.assertEqual(protocol.status(root), protocol.status(imported))
        self.assertEqual(protocol.accept_attempt(root, source_root=ROOT), protocol.accept_attempt(imported, source_root=ROOT))

    def test_wrong_archive_mode_and_dedicated_native_failure_are_replayed(self):
        with self.assertRaisesRegex(ValueError, 'Archive differs'):
            protocol.accept_attempt(self.qualified(corrupt_archive=True), source_root=ROOT)
        with self.assertRaisesRegex(ValueError, 'Failed native test'):
            protocol.accept_attempt(self.qualified(native_result='failed one\n'), source_root=ROOT)

    def test_ap_executor_architecture_is_separate_from_capture_architecture(self):
        root = self.qualified(skip_ap=True)
        profile = copy.deepcopy(self.profile)
        profile.update(architecture='aarch64', triple=protocol.TARGETS['aarch64'], hostname='replay-host')
        captures = protocol.load(root / 'outputs/captures/report.json')
        self.receipt(root, 'ap', {'ap_version': '0.7.35', 'package_sha256': captures['package_sha256'],
                     'capture_manifest_sha256': captures['capture_manifest_sha256']}, replay_profile=profile)
        accepted = protocol.accept_attempt(root, source_root=ROOT)
        self.assertEqual(accepted['architecture'], 'x86_64')
        execution = protocol.load(root / 'receipts/ap.json')['input']['execution']
        self.assertEqual(execution['architecture'], 'aarch64')

    def test_hook_arrays_are_normalized_and_blocking_cleanup_is_preserved(self):
        root = self.attempt()
        self.receipt(root, 'package', {}, raw_cleanup={'outcome': 'clean', 'owned_remaining': [], 'unknown': []})
        self.assertEqual(protocol.status(root)['refused'], [])
        root = self.attempt()
        cleanup = {'outcome': 'blocked', 'owned_remaining': 1, 'unknown': 0, 'report': 'outputs/package/cleanup.json'}
        self.receipt(root, 'package', {}, cleanup=cleanup,
                     raw_cleanup={'outcome': 'blocked', 'owned_remaining': [{'pid': 123}], 'unknown': []})
        self.assertEqual(protocol.status(root)['refused'], ['package'])

    def test_only_bound_native_elf_artifacts_can_execute_inside_attempt(self):
        root = self.attempt()
        binary = root / 'outputs/native/test'
        binary.parent.mkdir()
        binary.write_bytes(packaging.elf(62))
        binary.chmod(0o755)
        record = protocol.file_record(binary)
        with self.assertRaisesRegex(ValueError, 'lacks a prior'):
            protocol._executed(self.profile, [str(binary)], root=root, prior={})
        prior = {'frontend': {'outputs': {'outputs/native/test': record}}}
        self.assertEqual(protocol._executed(self.profile, [str(binary)], root=root, prior=prior), {str(binary): record})
        copied = root / 'outputs/native/copied'
        shutil.copy2(binary, copied)
        protocol._executed(self.profile, [str(copied)], root=root, prior=prior)
        script = root / 'outputs/native/generated.py'
        script.write_text('print("not an ELF")\n')
        with self.assertRaisesRegex(ValueError, 'native ELF'):
            protocol._executed(self.profile, [str(script)], root=root,
                prior={'frontend': {'outputs': {'outputs/native/generated.py': protocol.file_record(script)}}})
        wrong_arch = root / 'outputs/native/arm'
        wrong_arch.write_bytes(packaging.elf(183))
        with self.assertRaisesRegex(ValueError, 'native ELF'):
            protocol._executed(self.profile, [str(wrong_arch)], root=root, prior=prior)

    def test_same_stage_compiler_artifact_requires_exact_source_and_compiler(self):
        root = self.attempt()
        binary = root / 'outputs/test-binary'
        binary.write_bytes(packaging.elf(62))
        compiler = self.base / 'rustc'
        compiler.write_text('pinned compiler fixture')
        profile = copy.deepcopy(self.profile)
        profile['toolchain']['rustc'] = str(compiler)
        proof = {'architecture': 'x86_64', 'plus_source_commit': self.plan['plus_source_commit'],
                 'tooling_commit': self.plan['tooling_commit'], 'target': profile['triple'],
                 'source_sha256': protocol.digest({name: self.plan[name] for name in ('upstream', 'patch', 'plus_source_commit')}),
                 'compiler_sha256': protocol.file_record(compiler)['sha256'],
                 'files': {'outputs/test-binary': protocol.file_record(binary)}, 'cargo_artifact': {'reason': 'compiler-artifact'}}
        protocol._executed(profile, [str(binary)], root=root, compiler_artifacts=proof, plan=self.plan)
        proof['source_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'source/target'):
            protocol._executed(profile, [str(binary)], root=root, compiler_artifacts=proof, plan=self.plan)

    def test_packaged_launcher_must_match_pinned_source_and_package_manifest(self):
        root = self.attempt()
        launcher = root / 'outputs/package/bin/codex-plus'
        launcher.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'bin/codex-plus', launcher)
        manifest = {'release': self.plan['release'], 'plus_commit': self.plan['plus_source_commit'],
                    'target': self.profile['triple'], 'files': {'bin/codex-plus': protocol.file_record(launcher)}}
        self.put(root, 'outputs/package/codex-plus.json', manifest)
        protocol._executed(self.profile, [str(launcher)], root=root, plan=self.plan)
        launcher.write_text('#!/bin/sh\n# attempt-specific replacement\n')
        manifest['files']['bin/codex-plus'] = protocol.file_record(launcher)
        self.put(root, 'outputs/package/codex-plus.json', manifest)
        with self.assertRaisesRegex(ValueError, 'pinned source/manifest'):
            protocol._executed(self.profile, [str(launcher)], root=root, plan=self.plan)

    def test_import_rejects_path_escape_and_symlinks_with_valid_transport_hash(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)]:
            bundle = self.base / ('unsafe-' + kind.decode() + '.tar')
            with tarfile.open(bundle, 'w') as tar:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/outside'
                tar.addfile(member)
            with self.assertRaises(ValueError):
                protocol.import_attempt(bundle, self.base / ('unsafe-import-' + kind.decode()),
                                        expected_sha256=protocol.file_record(bundle)['sha256'])

    def test_handoff_refuses_unaccepted_rehearsal_and_missing_native_target(self):
        accepted = protocol.accept_attempt(self.qualified(), source_root=ROOT)
        with self.assertRaises(ValueError):
            protocol.make_handoff(self.plan, {'x86_64': accepted})
        other = copy.deepcopy(accepted)
        other.update(architecture='aarch64', rehearsal=True)
        with self.assertRaises(ValueError):
            protocol.make_handoff(self.plan, {'x86_64': accepted, 'aarch64': other})


if __name__ == '__main__':
    unittest.main()
