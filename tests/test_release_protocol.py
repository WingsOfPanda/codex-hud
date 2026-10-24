"""Reject incomplete or changed native evidence without launching release work."""
import copy
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

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

    def finalize_socket(self, root):
        if (root / 'socket-cleaned.json').exists():
            return
        metadata, _, _ = protocol.read_attempt(root)
        Path(metadata['socket_root']).rmdir()
        raw = {'outcome': 'clean',
            'owned_remaining': [], 'unknown': [], 'socket_identity': metadata['socket_identity'],
            'socket_removed': True}
        if (root / 'recovery/receipt.json').exists():
            raw.update(recovery_receipt='recovery/receipt.json',
                       recovery_receipt_sha256=protocol.file_record(root / 'recovery/receipt.json')['sha256'])
        report = self.put(root, 'outputs/final-cleanup.json', raw)
        protocol.seal_socket_cleanup(root, report)

    def export(self, root, destination):
        self.finalize_socket(root)
        return protocol.export_attempt(root, destination)

    def allocate_compat_short(self, root):
        metadata, _, _ = protocol.read_attempt(root)
        scope = root / 'compat'
        scope.mkdir()
        short = Path(self.profile['short_root']) / ('Q2' if Path(metadata['socket_root']).name == 'Q1' else 'Q1')
        short.mkdir(mode=0o700)
        info = short.stat()
        identity = {'path': str(short), 'dev': info.st_dev, 'ino': info.st_ino,
                    'uid': info.st_uid, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        self.put(scope, 'short-state.json', {'path': str(short), 'device': info.st_dev,
            'inode': info.st_ino, 'uid': info.st_uid, 'boot_id': identity['boot_id'], 'state': 'allocated'})
        self.put(scope, 'unreadable-baseline.json', {'boot_id': identity['boot_id'], 'processes': {}})
        self.put(scope, 'short-binding.json', {'path': str(short), 'device': info.st_dev, 'inode': info.st_ino,
            'baseline_sha256': protocol.file_record(scope / 'unreadable-baseline.json')['sha256']})
        self.put(scope, 'owned-process-identities.jsonl', '')
        return identity

    def finish_compat_short(self, root, identity, *, socket_removed=False):
        ownership = protocol._module(ROOT / 'scripts/release_ownership.py')
        ownership._remove_socket(root / 'compat', Path(identity['path']), identity, protocol)
        self.put(root, 'outputs/compat-cleanup.json', {'passed': True, 'outcome': 'clean',
            'owned_remaining': [], 'unknown': [], 'short_removed': True, 'short_identity': identity})
        metadata, _, _ = protocol.read_attempt(root)
        self.put(root, 'outputs/compat-root-cleanup.json', {'passed': True, 'outcome': 'clean',
            'owned_remaining': [], 'unknown': [], 'socket_removed': socket_removed,
            'socket_identity': metadata['socket_identity']})
        self.put(root, 'outputs/compat-combined-cleanup.json', {'outcome': 'clean',
            'owned_remaining': 0, 'unknown': 0, 'report': 'outputs/compat-combined-cleanup.json',
            'audits': [{'outcome': 'clean', 'owned_remaining': 0, 'unknown': 0, 'report': name}
                      for name in ('outputs/compat-cleanup.json', 'outputs/compat-root-cleanup.json')]})

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

    def qualified(self, *, rehearsal=False, skip_ap=False, native_result='ok one\n', corrupt_archive=False,
                  filtered=0, package_only=False, finalize=True):
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
                          f'test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; {filtered} filtered out; finished in 0s\n')
        logfile = self.put(root, 'outputs/native-tests/logfile', native_result)
        self.receipt(root, 'native-tests', {'suites': {'hud': {'listed': listed, 'stdout': stdout,
                     'logfile': logfile, 'exit_code': 0}}}, [listed, stdout, logfile])
        if package_only:
            if finalize:
                self.finalize_socket(root)
            return root
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
        if finalize:
            self.finalize_socket(root)
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
        prefix, patch_version = self.plan['plus_version'].rsplit('.', 1)
        next_version = f'{prefix}.{int(patch_version) + 1}'
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
        identity = protocol.load(first / 'attempt.json')['socket_identity']
        info = socket_root.stat()
        self.assertEqual((identity['dev'], identity['ino'], identity['uid']), (info.st_dev, info.st_ino, info.st_uid))
        self.assertEqual(identity['boot_id'], Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        self.assertEqual(protocol.load(first / 'socket-creation-results/00.json'), identity)
        self.assertEqual(protocol.load(first / 'socket-creation-intents/00.json')['path'], str(socket_root))
        with self.assertRaises(FileExistsError):
            protocol.write_once(first / 'attempt.json', {})
        self.assertEqual(protocol.status(first)['missing'], list(protocol.GATES))

    def test_socket_creation_failure_preserves_intent_and_completed_identity_survives_cleanup(self):
        with patch.object(protocol.secrets, 'choice', return_value='A'):
            root = self.attempt()
            with self.assertRaisesRegex(ValueError, 'namespace is full'):
                self.attempt()
        attempts = list(root.parent.iterdir())
        failed = next(path for path in attempts if path != root)
        failure = protocol.load(failed / 'initialization-failure.json')
        self.assertEqual(failure['last_intent'], 'socket-creation-intents/63.json')
        self.assertTrue((failed / failure['last_intent']).is_file())
        self.assertFalse((failed / 'attempt.json').exists())
        Path(protocol.load(root / 'attempt.json')['socket_root']).rmdir()
        protocol.read_attempt(root)

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
            record = self.export(root, bundle)
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
        record = self.export(root, bundle)
        with self.assertRaisesRegex(ValueError, 'Transport'):
            protocol.import_attempt(bundle, self.base / 'bad', expected_sha256='0' * 64)
        imported = protocol.import_attempt(bundle, self.base / 'imported', expected_sha256=record['sha256'])
        self.assertEqual(protocol.status(root)['receipts'], protocol.status(imported)['receipts'])
        self.assertTrue(protocol.status(imported)['evidence_only'])
        self.assertEqual((imported / 'outputs/native-tests/logfile').stat().st_mode & 0o222, 0)
        self.assertEqual(imported.stat().st_mode & 0o222, 0)
        with self.assertRaisesRegex(ValueError, 'separate actual archive'):
            protocol.accept_attempt(imported, source_root=ROOT)
        self.assertEqual(protocol.accept_attempt(root, source_root=ROOT),
                         protocol.accept_attempt(imported, source_root=ROOT, archive=root / 'outputs/candidate.tar.gz'))

    def test_export_excludes_archive_native_package_and_unreferenced_heavy_trees(self):
        root = self.qualified()
        self.put(root, 'upstream/unreferenced-source.rs', 'do not transfer source\n')
        self.put(root, 'cache/unreferenced-cache', 'do not transfer cache\n')
        self.put(root, 'outputs/unreferenced-binary', 'not referenced\n')
        bundle = self.base / 'lean.tgz'
        record = self.export(root, bundle)
        with tarfile.open(bundle) as tar:
            names = tar.getnames()
            manifest = json.load(tar.extractfile('evidence-export.json'))
        self.assertNotIn('outputs/candidate.tar.gz', names)
        self.assertFalse(any('codex-plus-fixture/' in name or name.startswith(('upstream/', 'cache/')) for name in names))
        self.assertNotIn('outputs/unreferenced-binary', names)
        self.assertEqual(manifest['external_artifacts']['outputs/candidate.tar.gz']['kind'], 'archive')
        imported = protocol.import_attempt(bundle, self.base / 'lean-import', expected_sha256=record['sha256'])
        with self.assertRaisesRegex(ValueError, 'cannot run or resume'):
            protocol.require_native_attempt(imported)
        bad_archive = self.base / 'wrong.tar.gz'
        bad_archive.write_bytes(b'not the accepted archive')
        with self.assertRaisesRegex(ValueError, 'archive hash/size'):
            protocol.accept_attempt(imported, source_root=ROOT, archive=bad_archive)
        with self.assertRaises((ValueError, OSError)):
            protocol.accept_attempt(imported, source_root=ROOT, archive=self.base / 'missing.tar.gz')

    def test_exported_raw_evidence_cannot_be_tampered_missing_or_made_external(self):
        for mutation in ('tampered', 'missing', 'externalized'):
            root = self.qualified()
            bundle = self.base / (root.name + '.tgz')
            record = self.export(root, bundle)
            imported = protocol.import_attempt(bundle, self.base / ('import-' + root.name), expected_sha256=record['sha256'])
            path = imported / 'outputs/native-tests/logfile'
            if mutation == 'tampered':
                path.chmod(0o600)
                path.write_text('ok forged\n')
            else:
                path.parent.chmod(0o700)
                path.unlink()
            if mutation == 'externalized':
                manifest = protocol.load(imported / 'evidence-export.json')
                manifest['external_artifacts']['outputs/native-tests/logfile'] = {
                    'kind': 'native-output', 'record': manifest['included'].pop('outputs/native-tests/logfile')}
                marker = imported / 'evidence-export.json'
                marker.chmod(0o600)
                marker.write_text(json.dumps(manifest))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                protocol.accept_attempt(imported, source_root=ROOT, archive=root / 'outputs/candidate.tar.gz')

    def test_focused_native_suite_may_have_filtered_tests(self):
        self.assertTrue(protocol.accept_attempt(self.qualified(filtered=23), source_root=ROOT)['accepted'])

    def test_export_requires_final_socket_removal_and_hash_bound_clean_report(self):
        root = self.attempt()
        bundle = self.base / 'unsealed.tgz'
        with self.assertRaisesRegex(ValueError, 'Socket directory remains'):
            protocol.export_attempt(root, bundle)
        self.assertFalse(bundle.exists())
        self.finalize_socket(root)
        raw = root / 'outputs/final-cleanup.json'
        raw.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'cleanup report changed'):
            protocol.export_attempt(root, bundle)
        self.assertFalse(bundle.exists())

    def test_cli_evidence_roundtrip_refusals_and_handoff(self):
        def cli(*args, success=True):
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/release_protocol.py'),
                                     *map(str, args)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
            return json.loads(result.stdout)
        root = self.qualified(finalize=False)
        plan_path = self.base / 'plan.json'
        plan_path.write_text(json.dumps(self.plan))
        self.assertTrue(cli('plan-check', '--plan', plan_path, '--source-root', ROOT)['valid'])
        self.assertEqual(cli('status', '--attempt', root)['attempt'], root.name)
        bundle = self.base / 'cli.tgz'
        cli('export', '--attempt', root, '--output', bundle, success=False)
        self.finalize_socket(root)
        exported = cli('export', '--attempt', root, '--output', bundle)
        imported = self.base / 'cli-import'
        cli('import', '--bundle', bundle, '--sha256', '0' * 64, '--output', imported, success=False)
        self.assertTrue(cli('import', '--bundle', bundle, '--sha256', exported['sha256'],
                            '--output', imported)['evidence_only'])
        accepted_path = self.base / 'accepted-x86.json'
        cli('accept', '--attempt', imported, '--source-root', ROOT, '--output', accepted_path, success=False)
        accepted = cli('accept', '--attempt', imported, '--source-root', ROOT,
                       '--archive', root / 'outputs/candidate.tar.gz', '--output', accepted_path)
        before = accepted_path.read_bytes()
        cli('accept', '--attempt', imported, '--source-root', ROOT,
            '--archive', root / 'outputs/candidate.tar.gz', '--output', accepted_path, success=False)
        self.assertEqual(accepted_path.read_bytes(), before)
        output = self.base / 'handoff.json'
        cli('handoff', '--plan', plan_path, '--source-root', ROOT, '--accept', f'x86_64={accepted_path}',
            '--output', output, success=False)
        other = copy.deepcopy(accepted)
        other.update(architecture='aarch64')
        other['target']['triple'] = protocol.TARGETS['aarch64']
        other_path = self.base / 'accepted-arm-fixture.json'
        other_path.write_text(json.dumps(other))
        handoff = cli('handoff', '--plan', plan_path, '--source-root', ROOT,
                      '--accept', f'x86_64={accepted_path}', '--accept', f'aarch64={other_path}', '--output', output)
        self.assertEqual(set(handoff['targets']), set(protocol.TARGETS))

    def test_validator_loading_does_not_create_bytecode_in_sealed_source(self):
        path = self.base / 'validator.py'
        path.write_text('VALUE = 1\n')
        self.assertEqual(protocol._module(path).VALUE, 1)
        self.assertFalse((self.base / '__pycache__').exists())

    def test_acceptance_requires_final_native_cleanup_before_and_after_sealing(self):
        root = self.qualified(finalize=False)
        with self.assertRaisesRegex(ValueError, 'Socket directory remains'):
            protocol.accept_attempt(root, source_root=ROOT)
        self.finalize_socket(root)
        self.assertTrue(protocol.accept_attempt(root, source_root=ROOT)['accepted'])
        Path(protocol.load(root / 'attempt.json')['socket_root']).mkdir()
        with self.assertRaisesRegex(ValueError, 'Socket directory remains'):
            protocol.accept_attempt(root, source_root=ROOT)

    def test_package_approval_qualifies_native_package_before_compat_without_qualifying_handoff(self):
        root = self.qualified(package_only=True, finalize=False)
        with self.assertRaisesRegex(ValueError, 'Socket directory remains'):
            protocol.approve_package(root, source_root=ROOT)
        self.finalize_socket(root)
        approval = protocol.approve_package(root, source_root=ROOT)
        self.assertTrue(all(approval[name] is True for name in ('accepted', 'cleanup_passed', 'native')))
        self.assertEqual(approval['source'], self.plan['plus_source_commit'])
        self.assertEqual(approval['archive_sha256'], protocol.file_record(root / 'outputs/candidate.tar.gz')['sha256'])
        self.assertEqual(set(approval['receipts']), {'package', 'native-tests'})
        with self.assertRaisesRegex(ValueError, 'Missing'):
            protocol.accept_attempt(root, source_root=ROOT)
        with self.assertRaises(ValueError):
            protocol.make_handoff(self.plan, {'x86_64': approval, 'aarch64': approval})
        output = self.base / 'package-approval.json'
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/release_protocol.py'),
            'approve-package', '--attempt', str(root), '--source-root', str(ROOT), '--output', str(output)],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(protocol.load(output), approval)
        with self.assertRaisesRegex(ValueError, 'Failed native test'):
            protocol.approve_package(self.qualified(package_only=True, native_result='failed one\n'), source_root=ROOT)

    def test_coordinator_ap_receipt_qualifies_readonly_import_without_modifying_it(self):
        source = self.base / 'ap-source'
        source.mkdir()
        node = self.base / 'node'
        node.write_text('pinned Node fixture\n')
        bundle = self.base / 'observer-bundle.js'
        bundle.write_text('pinned installed observer\n')
        self.plan['compat']['ap_replay'] = {'source_commit': 'd' * 40,
            'installed_plugin_commit': 'e' * 40, 'bundle_sha256': protocol.file_record(bundle)['sha256']}
        root = self.qualified(skip_ap=True)
        self.finish_compat_short(root, self.allocate_compat_short(root))
        self.assertEqual(protocol.status(root)['next_action'], 'record-ap')
        self.assertEqual(protocol._ap_binding(root)['attempt'], root.name)
        evidence = self.base / 'native-evidence.tgz'
        exported = self.export(root, evidence)
        imported = protocol.import_attempt(evidence, self.base / 'native-import', expected_sha256=exported['sha256'])
        before = {path.relative_to(imported).as_posix(): protocol.file_record(path)
                  for path in imported.rglob('*') if path.is_file()}
        profile = copy.deepcopy(self.profile)
        profile.update(source_root=str(source), architecture='aarch64', triple=protocol.TARGETS['aarch64'],
                       hostname='coordinator', system_tools={str(node): protocol.file_record(node)['sha256']},
                       ap_replay={'node': str(node), 'node_modules': str(source / 'node_modules'),
                                  'installed_bundle': str(bundle), 'installed_registry': str(self.base / 'registry.json')})
        coordinator = self.base / 'ap-evidence'
        coordinator.mkdir()
        self.put(coordinator, 'preparation.json', {'ap_replay': self.plan['compat']['ap_replay']})
        self.put(coordinator, 'raw.json', {'tests': 23, 'matching_verdicts': 20, 'changed_pairs': 2})
        self.put(coordinator, 'cleanup.json', {'outcome': 'clean', 'owned_remaining': [], 'unknown': []})
        captures = protocol.load(imported / 'outputs/captures/report.json')
        self.put(coordinator, 'report.json', {'architecture': 'x86_64',
            'plus_source_commit': self.plan['plus_source_commit'], 'ap_version': self.plan['compat']['ap_version'],
            'ap_replay': self.plan['compat']['ap_replay'], 'package_sha256': captures['package_sha256'],
            'capture_manifest_sha256': captures['capture_manifest_sha256'],
            'checks': {'ap-check': {'outcome': 'passed',
                                   'evidence': {'raw.json': protocol.file_record(coordinator / 'raw.json')['sha256']}}}})
        execution = {'job_id': 'coordinator-ap-job', 'executor_package_root': profile['executor_package_root'],
            'executor_sha256': 'c' * 64, 'architecture': 'aarch64', 'plus_source_commit': self.plan['plus_source_commit'],
            'tooling_commit': self.plan['tooling_commit'], 'outcome': 'passed', 'exit_code': 0,
            'argv': [str(node), 'pinned-replay'], 'cwd': str(source), 'env': {},
            'replay_profile_sha256': protocol.digest(profile)}
        kwargs = dict(inputs=['preparation.json'], executed=[str(node)],
            outputs=['report.json', 'raw.json', 'cleanup.json'], evidence='report.json', execution=execution,
            cleanup={'outcome': 'clean', 'owned_remaining': 0, 'unknown': 0, 'report': 'cleanup.json'},
            replay_profile=profile)
        protocol.write_ap_receipt(imported, coordinator, **kwargs)
        sidecar = coordinator / 'ap-receipt.json'
        accepted = protocol.accept_attempt(imported, source_root=ROOT, archive=root / 'outputs/candidate.tar.gz',
                                           ap_receipt=sidecar)
        self.assertEqual(set(accepted['receipts']), set(protocol.GATES))
        self.assertEqual(accepted['architecture'], 'x86_64')
        with self.assertRaises(FileExistsError):
            protocol.write_ap_receipt(imported, coordinator, **kwargs)
        after = {path.relative_to(imported).as_posix(): protocol.file_record(path)
                 for path in imported.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        (coordinator / 'raw.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Evidence output changed'):
            protocol.accept_attempt(imported, source_root=ROOT, archive=root / 'outputs/candidate.tar.gz', ap_receipt=sidecar)

    def test_fixture_import_and_previous_package_pins_are_exact(self):
        module = self.base / 'pyte/__init__.py'
        module.parent.mkdir()
        module.write_text('fixture import\n')
        profile = copy.deepcopy(self.profile)
        profile['python'].update(fixture_sites=[str(self.base)],
            fixture_imports={'pyte': {'origin': str(module), 'sha256': protocol.file_record(module)['sha256']}})
        protocol.validate_profile(profile, 'x86_64')
        protocol._executed(profile, [str(module)])
        module.write_text('changed fixture import\n')
        with self.assertRaisesRegex(ValueError, 'Fixture module pin'):
            protocol._executed(profile, [str(module)])
        previous = self.base / 'previous-package'
        binary = previous / 'bin/codex'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(packaging.elf(62))
        manifest = {'release': self.plan['previous_release']['tag'], 'target': profile['triple'],
            'plus_commit': self.plan['compat']['previous_source_commit'],
            'tree_sha256': self.plan['previous_release']['targets']['x86_64']['tree_sha256'],
            'files': {'bin/codex': protocol.file_record(binary)}}
        self.put(previous, 'codex-plus.json', manifest)
        manifest_sha = protocol.file_record(previous / 'codex-plus.json')['sha256']
        self.plan['previous_release']['targets']['x86_64']['manifest_sha256'] = manifest_sha
        proof = {'path': str(previous), 'manifest_sha256': manifest_sha}
        protocol._executed(profile, [str(binary)], plan=self.plan, previous_package=proof)
        binary.write_bytes(packaging.elf(183))
        with self.assertRaisesRegex(ValueError, 'qualified inventory'):
            protocol._executed(profile, [str(binary)], plan=self.plan, previous_package=proof)

    def forensic_recovery(self, saved_finished=None, finished=None):
        root = self.attempt()
        self.receipt(root, 'package', {}, outcome='failed', exit_code=9)
        metadata, _, _ = protocol.read_attempt(root)
        self.put(root, 'process-journal.jsonl', '{"event":"started"}\n')
        self.put(root, 'ownership-baseline.json', {'processes': {}})
        self.put(root, 'native-preparation.json', {'sealed': 'fixture'})
        self.put(root, 'native-starts/preflight.json', {'started': True})
        binding = {'attempt': metadata['attempt'], 'attempt_root': str(root),
            'plan_sha256': protocol.digest(self.plan), 'socket_identity': metadata['socket_identity'],
            'journal_sha256': protocol.file_record(root / 'process-journal.jsonl')['sha256'],
            'ownership_baseline_sha256': protocol.file_record(root / 'ownership-baseline.json')['sha256']}
        resolution = {'cleanup_verified': True, 'release_recovery': dict(binding, subtree_cleanup_evidence='explicit root proof')}
        result = {'id': 'job', 'root': 'root', 'state': 'failed', 'exit_code': 9,
                  'cleanup': {'status': 'failed', 'exit_code': 1}}
        saved_result = copy.deepcopy(result)
        if saved_finished is not None:
            saved_result['finished_at'] = saved_finished
            result['finished_at'] = finished
        records = {'recovery/managed-state.json': {'result': saved_result, 'resolution': resolution},
                   'recovery/managed-result.json': result, 'recovery/managed-resolution.json': resolution}
        for name, value in records.items():
            self.put(root, name, value)
        names = [*records, 'process-journal.jsonl', 'ownership-baseline.json',
                 'native-preparation.json', 'native-starts/preflight.json']
        self.put(root, 'recovery/receipt.json', {'binding': binding,
            'files': {name: protocol.file_record(root / name) for name in names},
            'absent': [], 'audit': {'passed': True}, 'seals_verified': True,
            'qualification_unchanged': True, 'result_absent': False})
        for name in ('socket-removal-intent.json', 'socket-quarantine.json', 'socket-moved.json',
                     'socket-deleted.json', 'socket-cleanup-retries/01-intent.json'):
            self.put(root, name, {'identity': metadata['socket_identity']})
        return root, names + ['recovery/receipt.json', 'socket-cleanup-retries/01-intent.json',
                              'socket-deleted.json']

    def test_forensic_recovery_export_preserves_raw_records_and_original_failure(self):
        lower, upper = 1792751962.6755495, 1792751962.6755497
        for index, (saved, separate) in enumerate([(None,None), (lower,upper), (upper,lower)]):
            with self.subTest(saved=saved, separate=separate):
                root, names = self.forensic_recovery(saved, separate)
                before = {name: (root / name).read_bytes() for name in names}
                bundle = self.base / f'recovered-failure-{index}.tgz'
                exported = self.export(root, bundle)
                imported = protocol.import_attempt(bundle, self.base / f'recovered-import-{index}',
                                                   expected_sha256=exported['sha256'])
                for attempt in (root, imported):
                    verdict = protocol.status(attempt)
                    self.assertEqual(verdict['refused'], ['package'])
                    self.assertEqual(verdict['next_action'], 'review-failure')
                    self.assertEqual(before, {name: (attempt / name).read_bytes() for name in names})
                    with self.assertRaisesRegex(ValueError, 'Missing/failed'):
                        protocol.approve_package(attempt, source_root=ROOT,
                                                 archive=self.base / 'not-present.tar.gz')
                (root / 'process-journal.jsonl').write_text('changed journal')
                with self.assertRaisesRegex(ValueError, 'Evidence output changed'):
                    protocol.status(root)

    def test_forensic_recovery_rejects_result_drift_and_tampering(self):
        lower, upper = 1792751962.6755495, 1792751962.6755497
        cases = [('finished_at',math.nextafter(upper,math.inf)),
                 ('finished_at',math.nextafter(math.nextafter(lower,-math.inf),-math.inf)),
                 ('exit_code',0), ('state','completed'), ('id','other'), ('root','other'),
                 ('cleanup',{'status': 'completed', 'exit_code': 0}), ('extra',True),
                 ('hash',None)]
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key, value=value):
                root, names = self.forensic_recovery(lower, upper)
                result_name = 'recovery/managed-result.json'
                result = protocol.load(root / result_name)
                result[key] = value
                self.put(root, result_name, result)
                if key != 'hash':
                    receipt = protocol.load(root / 'recovery/receipt.json')
                    receipt['files'][result_name] = protocol.file_record(root / result_name)
                    self.put(root, 'recovery/receipt.json', receipt)
                before = {name: (root / name).read_bytes() for name in names}
                message = 'Evidence output changed' if key == 'hash' else 'managed snapshots disagree'
                with self.assertRaisesRegex(ValueError, message):
                    protocol.status(root)
                with self.assertRaisesRegex(ValueError, message):
                    protocol.export_attempt(root, self.base / f'invalid-recovery-{index}.tgz')
                self.assertEqual(before, {name: (root / name).read_bytes() for name in names})

    def test_pkg_config_profile_is_optional_and_requires_exact_scoped_input_pins(self):
        profile = copy.deepcopy(self.profile)
        paths = ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache', 'license_cache', 'official_binary')
        profile['native_build'] = {name: str(self.base / name) for name in paths}
        profile['native_build'].update(cargo_jobs=2, test_threads=2, official_sha256='f' * 64)
        protocol.validate_profile(profile, 'x86_64')
        scoped = {'path': str(self.base / 'pkgconfig'),
                  'files': {str(self.base / 'pkgconfig/libcap.pc'): 'a' * 64,
                            str(self.base / 'include/sys/capability.h'): 'b' * 64,
                            str(self.base / 'lib/libcap.so'): 'c' * 64}}
        profile['native_build']['pkg_config'] = scoped
        protocol.validate_profile(profile, 'x86_64')
        for value in ({'path': 'relative', 'files': scoped['files']},
                      {'path': scoped['path'], 'files': {}},
                      {'path': scoped['path'], 'files': {'relative.pc': 'a' * 64}},
                      {'path': scoped['path'], 'files': {str(self.base / 'bad.pc'): 'invalid'}},
                      dict(scoped, env={'PKG_CONFIG_PATH': 'unreviewed'})):
            invalid = copy.deepcopy(profile)
            invalid['native_build']['pkg_config'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                protocol.validate_profile(invalid, 'x86_64')

    def test_new_compat_allocation_blocks_acceptance_and_export_after_native_seal(self):
        root = self.qualified()
        identity = self.allocate_compat_short(root)
        before = (root / 'compat/short-state.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'Compatibility short directory remains'):
            protocol.accept_attempt(root, source_root=ROOT)
        with self.assertRaisesRegex(ValueError, 'Compatibility short directory remains'):
            protocol.approve_package(root, source_root=ROOT)
        bundle = self.base / 'blocked-compat.tgz'
        with self.assertRaisesRegex(ValueError, 'Compatibility short directory remains'):
            protocol.export_attempt(root, bundle)
        self.assertFalse(bundle.exists())
        self.assertTrue(Path(identity['path']).is_dir())
        self.assertEqual((root / 'compat/short-state.json').read_bytes(), before)

    def test_shared_compat_removal_and_independent_audits_survive_readonly_export(self):
        root = self.qualified()
        identity = self.allocate_compat_short(root)
        self.finish_compat_short(root, identity)
        before = {path.relative_to(root).as_posix(): path.read_bytes()
                  for path in root.rglob('*') if path.is_file()}
        self.assertEqual(protocol.status(root)['next_action'], 'accept')
        self.assertTrue(protocol.accept_attempt(root, source_root=ROOT)['accepted'])
        bundle = self.base / 'compat-clean.tgz'
        exported = protocol.export_attempt(root, bundle)
        with tarfile.open(bundle) as tar:
            names = tar.getnames()
        for name in ('compat/short-state.json', 'compat/short-binding.json',
                     'compat/unreadable-baseline.json', 'compat/owned-process-identities.jsonl',
                     'compat/socket-removal-intent.json', 'compat/socket-quarantine.json',
                     'compat/socket-moved.json', 'compat/socket-deleted.json', 'compat/socket-removal.json',
                     'outputs/compat-cleanup.json', 'outputs/compat-root-cleanup.json',
                     'outputs/compat-combined-cleanup.json'):
            self.assertIn(name, names)
        # A coordinator verifies foreign cleanup records, not a coincidental local path.
        Path(identity['path']).mkdir()
        native_short = Path(protocol.load(root / 'attempt.json')['socket_root'])
        native_short.mkdir()
        imported = protocol.import_attempt(bundle, self.base / 'compat-import', expected_sha256=exported['sha256'])
        self.assertEqual(protocol.status(imported)['next_action'], 'accept')
        self.assertTrue(protocol.accept_attempt(imported, source_root=ROOT,
            archive=root / 'outputs/candidate.tar.gz')['accepted'])
        with self.assertRaisesRegex(ValueError, 'Socket directory remains'):
            protocol.accept_attempt(root, source_root=ROOT)
        native_short.rmdir()
        with self.assertRaisesRegex(ValueError, 'Compatibility short directory remains'):
            protocol.accept_attempt(root, source_root=ROOT)
        self.assertEqual(before, {path.relative_to(root).as_posix(): path.read_bytes()
                                 for path in root.rglob('*') if path.is_file()})

    def test_compat_root_audit_still_accepts_explicit_removal(self):
        root = self.qualified()
        self.finish_compat_short(root, self.allocate_compat_short(root), socket_removed=True)
        self.assertTrue(protocol.accept_attempt(root, source_root=ROOT)['accepted'])

    def test_compat_root_audit_requires_boolean_and_exact_clean_identity(self):
        for mutation in ('missing', None, 0, 1, 'false', [], {}, 'identity', 'owned', 'unknown'):
            with self.subTest(mutation=mutation):
                root = self.qualified()
                self.finish_compat_short(root, self.allocate_compat_short(root))
                raw = protocol.load(root / 'outputs/compat-root-cleanup.json')
                if mutation == 'missing':
                    raw.pop('socket_removed')
                elif mutation == 'identity':
                    raw['socket_identity']['ino'] += 1
                elif mutation in ('owned', 'unknown'):
                    raw['owned_remaining' if mutation == 'owned' else 'unknown'] = [{'pid': 999}]
                else:
                    raw['socket_removed'] = mutation
                self.put(root, 'outputs/compat-root-cleanup.json', raw)
                before = (root / 'outputs/compat-root-cleanup.json').read_bytes()
                with self.assertRaises(ValueError):
                    protocol.accept_attempt(root, source_root=ROOT)
                with self.assertRaises(ValueError):
                    protocol.export_attempt(root, self.base / (root.name + '.tgz'))
                self.assertEqual(before, (root / 'outputs/compat-root-cleanup.json').read_bytes())

    def test_compat_audit_without_new_removal_requires_sealed_native_removal(self):
        for mutation in ('missing-seal', 'seal-identity', 'seal-outcome', 'hash', 'report-identity',
                         'report-removed', 'report-unknown', 'live-path'):
            with self.subTest(mutation=mutation):
                root = self.qualified()
                self.finish_compat_short(root, self.allocate_compat_short(root))
                proof = protocol.load(root / 'socket-cleaned.json')
                report = protocol.load(root / proof['report'])
                if mutation == 'missing-seal':
                    (root / 'socket-cleaned.json').unlink()
                elif mutation == 'live-path':
                    Path(proof['socket_identity']['path']).mkdir()
                else:
                    if mutation == 'seal-identity':
                        proof['socket_identity']['ino'] += 1
                    elif mutation == 'seal-outcome':
                        proof['outcome'] = 'unknown'
                    elif mutation == 'report-identity':
                        report['socket_identity']['ino'] += 1
                    elif mutation == 'report-removed':
                        report['socket_removed'] = False
                    elif mutation == 'report-unknown':
                        report['unknown'] = [{'pid': 999}]
                    self.put(root, proof['report'], report)
                    proof['report_sha256'] = ('0' * 64 if mutation == 'hash' else
                        protocol.file_record(root / proof['report'])['sha256'])
                    (root / 'socket-cleaned.json').chmod(0o600)
                    self.put(root, 'socket-cleaned.json', proof)
                before = {path.relative_to(root).as_posix(): path.read_bytes()
                          for path in root.rglob('*') if path.is_file()}
                for verify in (protocol._compat_cleanup_proof,
                               lambda path: protocol.accept_attempt(path, source_root=ROOT),
                               lambda path: protocol.export_attempt(path, self.base / (root.name + '.tgz'))):
                    with self.assertRaises((OSError, ValueError)):
                        verify(root)
                self.assertEqual(before, {path.relative_to(root).as_posix(): path.read_bytes()
                                         for path in root.rglob('*') if path.is_file()})

    def test_compat_removal_identity_or_unknown_raw_audit_cannot_use_clean_summary(self):
        for mutation in ('identity', 'short-audit', 'root-audit'):
            root = self.qualified()
            identity = self.allocate_compat_short(root)
            self.finish_compat_short(root, identity)
            if mutation == 'identity':
                path = root / 'compat/socket-removal.json'
                raw = protocol.load(path)
                raw['identity']['ino'] += 1
                path.chmod(0o600)
            else:
                path = root / ('outputs/compat-cleanup.json' if mutation == 'short-audit'
                               else 'outputs/compat-root-cleanup.json')
                raw = protocol.load(path)
                raw.update(outcome='unknown', passed=False, unknown=[{'pid': 999, 'reason': 'unresolved'}])
            path.write_text(json.dumps(raw))
            before = path.read_bytes()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                protocol.accept_attempt(root, source_root=ROOT)
            with self.assertRaises(ValueError):
                protocol.export_attempt(root, self.base / (root.name + '.tgz'))
            self.assertEqual(path.read_bytes(), before)

    def test_import_requires_independent_compat_audit_even_with_valid_transport_digest(self):
        root = self.qualified()
        self.finish_compat_short(root, self.allocate_compat_short(root))
        bundle = self.base / 'complete-compat.tgz'
        protocol.export_attempt(root, bundle)
        omitted = 'outputs/compat-cleanup.json'
        invalid = self.base / 'missing-compat-audit.tgz'
        with tarfile.open(bundle) as source, tarfile.open(invalid, 'w:gz') as output:
            for member in source:
                if member.name == omitted:
                    continue
                payload = source.extractfile(member).read()
                if member.name == 'evidence-export.json':
                    manifest = json.loads(payload)
                    manifest['included'].pop(omitted)
                    payload = protocol.encoded(manifest)
                    member.size = len(payload)
                output.addfile(member, io.BytesIO(payload))
        destination = self.base / 'invalid-compat-import'
        with self.assertRaises((OSError, ValueError)):
            protocol.import_attempt(invalid, destination, expected_sha256=protocol.file_record(invalid)['sha256'])
        self.assertFalse(destination.exists())

    def test_wrong_archive_mode_and_dedicated_native_failure_are_replayed(self):
        with self.assertRaisesRegex(ValueError, 'Archive differs'):
            protocol.accept_attempt(self.qualified(corrupt_archive=True), source_root=ROOT)
        failed = self.qualified(native_result='failed one\n')
        self.finish_compat_short(failed, self.allocate_compat_short(failed))
        with self.assertRaisesRegex(ValueError, 'Failed native test'):
            protocol.accept_attempt(failed, source_root=ROOT)

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
        launcher.chmod(0o755)
        source_launcher = self.base / 'source-launcher/bin/codex-plus'
        source_launcher.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'bin/codex-plus', source_launcher)
        source_launcher.chmod(0o775)
        profile = copy.deepcopy(self.profile)
        profile['source_root'] = str(source_launcher.parents[1])
        manifest = {'release': self.plan['release'], 'plus_commit': self.plan['plus_source_commit'],
                    'target': self.profile['triple'], 'files': {'bin/codex-plus': protocol.file_record(launcher)}}
        self.put(root, 'outputs/package/codex-plus.json', manifest)
        protocol._executed(profile, [str(launcher)], root=root, plan=self.plan)
        launcher.write_text('#!/bin/sh\n# attempt-specific replacement\n')
        manifest['files']['bin/codex-plus'] = protocol.file_record(launcher)
        self.put(root, 'outputs/package/codex-plus.json', manifest)
        with self.assertRaisesRegex(ValueError, 'pinned source/manifest'):
            protocol._executed(profile, [str(launcher)], root=root, plan=self.plan)

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
