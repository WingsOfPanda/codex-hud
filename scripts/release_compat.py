"""Local native compatibility recipes, extracted once from the qualified closure.

Preparation writes data only. The native owner reviews and launches the returned
argv through its existing managed executor. No host transport or installation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys

RECIPE = Path(__file__).resolve().parent / 'release_recipes/compat'
UPSTREAM = {'version': '0.163.0', 'tag': 'rust-v0.163.0',
            'commit': '83086188ae0614bd882818ff1aa23f3194962012'}
GATES = ('quiet-rollback', 'advisor-nine-checks', 'actual-old-tui-root-result', 'packaged-cursor-v2')


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    with Path(path).open('x') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')


def validate_config(config):
    require(config['schema'] == 1 and config['recipe_revision'] == 1, 'Unsupported compatibility recipe')
    require(config['upstream'] == UPSTREAM, 'New upstream requires recipe qualification')
    require(config['architecture'] in ('x86_64', 'aarch64'), 'Unsupported native architecture')
    for key in ('source_commit', 'fixture_source_commit', 'frontend_compile_origin'):
        require(re.fullmatch('[0-9a-f]{40}', config[key]) and config[key] == config['source_commit'], 'Wrong source identity: ' + key)
    for key in ('new_manifest_sha256', 'new_archive_sha256', 'qualification_evidence_sha256', 'frontend_patch_sha256'):
        require(re.fullmatch('[0-9a-f]{64}', config[key]) and config[key] != '0' * 64, 'Invalid digest: ' + key)
    for key in ('scope', 'source', 'fixture_source', 'old_package', 'new_package', 'new_archive', 'stock_binary', 'qualification_evidence', 'short_root'):
        require(Path(config[key]).is_absolute() and '..' not in Path(config[key]).parts, 'Invalid absolute path: ' + key)
    require(config['source'] == config['fixture_source'], 'Fixtures must come from the accepted source')
    require(not Path(config['patch_path']).is_absolute() and '..' not in Path(config['patch_path']).parts, 'Unsafe patch path')
    require(config['root_accepted_final_package'] is True, 'Root package acceptance required')
    require(config['previous_release'] != config['release'] and config['release'].startswith(UPSTREAM['version'] + '-plus.'), 'Wrong release identity')
    require(config['previous_release'].startswith(UPSTREAM['version'] + '-plus.'), 'Rollback upstream differs')
    require(re.fullmatch('[0-9a-f]{40}', config['previous_identity']['commit']), 'Invalid previous source')
    for mapping in (config['previous_identity']['manifests'], config['stock_binary_sha256']):
        require(config['architecture'] in mapping and all(re.fullmatch('[0-9a-f]{64}', value) for value in mapping.values()), 'Missing native identity pin')
    require(bool(config['fixture_files']), 'Fixture closure missing')
    for name, sha in config['fixture_files'].items():
        require(not Path(name).is_absolute() and '..' not in Path(name).parts and re.fullmatch('[0-9a-f]{64}', sha), 'Unsafe fixture binding')
    require(config['recipe_hashes'] == {path.name: digest(path) for path in RECIPE.iterdir() if path.is_file()}, 'Recipe bytes changed')
    return config


def accepted_package(config):
    """Check the approval before creating any private fixture directories."""
    require(digest(config['qualification_evidence']) == config['qualification_evidence_sha256'], 'Approval digest mismatch')
    approved = json.loads(Path(config['qualification_evidence']).read_text())
    require(all(approved.get(name) is True for name in ('accepted', 'cleanup_passed', 'native')), 'Package is not accepted and clean')
    for actual, expected in (('source', 'source_commit'), ('architecture', 'architecture'),
                             ('archive_sha256', 'new_archive_sha256'), ('manifest_sha256', 'new_manifest_sha256')):
        require(approved[actual] == config[expected], 'Wrong approval ' + actual)
    require(digest(Path(config['new_package']) / 'codex-plus.json') == config['new_manifest_sha256'], 'Package manifest mismatch')
    manifest = json.loads((Path(config['new_package']) / 'codex-plus.json').read_text())
    require(manifest['release'] == config['release'] and manifest['plus_commit'] == config['source_commit'], 'Wrong package source/release')
    require(manifest['target'] == config['architecture'] + '-unknown-linux-gnu', 'Wrong package architecture')
    require(manifest['coordinator_protocol'] == 5 and manifest['execution_record_format'] == 1 and manifest['upstream'] == UPSTREAM, 'Unsupported package protocol')


def prepare(plan, profile, attempt, *, config, helper_binding):
    """Bind accepted packages and committed recipes; return the local launch data."""
    attempt = Path(attempt).resolve(strict=True)
    arch = profile['architecture']
    previous = plan['previous_release']
    compat = plan['compat']
    values = dict(config, schema=1, recipe_revision=compat['recipe_revision'],
        architecture=arch, source_commit=plan['plus_source_commit'],
        fixture_source_commit=plan['plus_source_commit'], frontend_compile_origin=plan['plus_source_commit'],
        source=profile['source_root'], fixture_source=profile['source_root'],
        release=plan['release'], previous_release=previous['tag'], upstream=plan['upstream'],
        patch_path=plan['patch']['path'], frontend_patch_sha256=plan['patch']['sha256'],
        short_root=profile['short_root'], scope=str(attempt / 'compat'),
        previous_identity={'commit': compat['previous_source_commit'],
            'manifests': {key: row['manifest_sha256'] for key, row in previous['targets'].items()}},
        stock_binary_sha256=compat['stock_binary_sha256'], fixture_files=compat['fixture_files'],
        gate_checks=plan['gates'][arch],
        recipe_hashes={path.name: digest(path) for path in sorted(RECIPE.iterdir()) if path.is_file()})
    validate_config(values)
    accepted_package(values)
    require(digest(values['new_archive']) == values['new_archive_sha256'], 'Archive digest mismatch')
    require(digest(values['stock_binary']) == values['stock_binary_sha256'][arch], 'Stock binary digest mismatch')
    source = Path(values['source'])
    require(subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == values['source_commit'], 'Wrong source checkout')
    require(digest(source / values['patch_path']) == values['frontend_patch_sha256'], 'Wrong frontend patch')
    for name, sha in values['fixture_files'].items():
        require(digest(source / name) == sha, 'Wrong fixture bytes: ' + name)
    require(tuple(values['gate_checks']['compat']['checks']) == GATES and
            values['gate_checks']['captures']['checks'] == ['native-cursor-states'], 'Unsupported compatibility checks')
    require(digest(helper_binding['helper']) == helper_binding['sha256'], 'Owned supervisor binding mismatch')
    scope = Path(values['scope'])
    scope.mkdir(mode=0o700)
    write(scope / 'native.config.json', values)
    write(scope / 'helper-binding.json', helper_binding)
    footer = json.loads((RECIPE / 'footer-contract.json').read_text())
    footer.update(release=plan['release'], frontend_patch_sha256=plan['patch']['sha256'], ap_release=compat['ap_version'])
    write(scope / 'footer-contract.json', footer)
    return {'scope': str(scope), 'argv': [profile['python']['path'], str(Path(__file__).resolve()), 'run', '--scope', str(scope)],
            'gates': list(GATES), 'native_gates_run': False}


def export_reports(scope):
    """Adapt passed raw native evidence to the coordinator's existing gate schema."""
    scope = Path(scope).resolve(strict=True)
    config = validate_config(json.loads((scope / 'native.config.json').read_text()))
    evidence = scope / 'evidence'
    raw = json.loads((evidence / 'report.json').read_text())
    require(raw['passed'] is True and raw['architecture'] == config['architecture'] and
            raw['source_commit'] == config['source_commit'], 'Native report identity mismatch')
    require(tuple(row['name'] for row in raw['stages']) == GATES and
            all(row['passed'] is True and row['exit_code'] == 0 for row in raw['stages']), 'Incomplete native gates')
    require(raw['configuration_sha256'] == digest(scope / 'native.config.json') and
            raw['gate_sha256'] == config['recipe_hashes']['verify-mixes.py'], 'Native recipe/configuration mismatch')
    require(raw['archive']['archive_sha256'] == config['new_archive_sha256'] and
            raw['packages']['new']['manifest_sha256'] == config['new_manifest_sha256'], 'Native package binding mismatch')
    for name, sha in raw['evidence_sha256'].items():
        require(not Path(name).is_absolute() and '..' not in Path(name).parts and digest(evidence / name) == sha, 'Native dedicated report changed')
    root = scope.parent
    reports = ('report.json', 'rollback/gate.json', 'advisor-nine-checks.json', 'old-tui-root-result/report.json')
    raw_bindings = {str((evidence / name).relative_to(root)): digest(evidence / name) for name in reports}
    captures = evidence / 'cursor-v2'
    manifest = json.loads((captures / 'manifest.json').read_text())
    integrity = json.loads((captures / 'integrity.json').read_text())
    require(manifest['candidate_acceptance'] is True and manifest['capture_complete'] is True and
            len(manifest['captures']) == 20 and integrity['passed'] is True, 'Incomplete native captures')
    files = {}
    for path in sorted(captures.rglob('*')):
        require(not path.is_symlink(), 'Capture contains a symlink')
        if path.is_file():
            info = path.stat()
            files[str(path.relative_to(root))] = {'sha256': digest(path), 'size': info.st_size,
                                                  'mode': f'{info.st_mode & 0o777:04o}'}
    inventory = scope / 'capture-manifest.json'
    write(inventory, {'architecture': config['architecture'], 'package_sha256': config['new_archive_sha256'], 'files': files})
    capture_bindings = {str(path.relative_to(root)): digest(path) for path in (captures / 'manifest.json', captures / 'integrity.json', inventory)}
    common = {'architecture': config['architecture'], 'plus_source_commit': config['source_commit']}
    compat_report = dict(common, checks={name: {'outcome': 'passed', 'evidence': raw_bindings | capture_bindings}
        for name in config['gate_checks']['compat']['checks']})
    captures_report = dict(common, package_sha256=config['new_archive_sha256'],
        capture_manifest=str(inventory.relative_to(root)), capture_manifest_sha256=digest(inventory),
        checks={name: {'outcome': 'passed', 'evidence': capture_bindings}
                for name in config['gate_checks']['captures']['checks']})
    write(scope / 'compat-report.json', compat_report)
    write(scope / 'captures-report.json', captures_report)
    return {'compat': str(scope / 'compat-report.json'), 'captures': str(scope / 'captures-report.json'),
            'capture_manifest': str(inventory)}


def run(scope, *, setup_only=False):
    """Run complete setup before gates using the pinned existing child supervisor."""
    scope = Path(scope).resolve(strict=True)
    config = validate_config(json.loads((scope / 'native.config.json').read_text()))
    require(platform.machine() == config['architecture'], 'Compatibility must run on the native host')
    accepted_package(config)
    env = dict(os.environ, CODEX_PLUS_COMPAT_SCOPE=str(scope), CODEX_PLUS_RELEASE_QA_SCOPE=str(scope), PYTHONDONTWRITEBYTECODE='1')
    # Import the same recipe adapter used by every setup and workload subprocess.
    os.environ.update(env)
    sys.path.insert(0, str(RECIPE))
    import owned_run
    from permission_cleanup import capture_baseline
    short_root = Path(config['short_root']).resolve(strict=True)
    require(not (scope / 'short-binding.json').exists(), 'Preserve previous attempt; prepare a fresh compatibility stage')
    for unused in range(4096):
        short = short_root / ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(2))
        try:
            short.mkdir(mode=0o700)
            break
        except FileExistsError:
            pass
    else:
        raise FileExistsError('No free short fixture directory')
    info = short.stat()
    write(scope / 'short-state.json', {'path': str(short), 'device': info.st_dev, 'inode': info.st_ino,
        'uid': info.st_uid, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'state': 'allocated'})
    baseline = capture_baseline([scope, short], scope)
    write(scope / 'unreadable-baseline.json', baseline)
    write(scope / 'short-binding.json', {'path': str(short), 'device': info.st_dev, 'inode': info.st_ino,
                                        'baseline_sha256': digest(scope / 'unreadable-baseline.json')})
    env['TMPDIR'] = str(short)
    os.environ['TMPDIR'] = str(short)
    (scope / 'setup-smoke').mkdir(mode=0o700)
    with (scope / 'setup-smoke.log').open('x') as log:
        result = owned_run.run([sys.executable, RECIPE / 'preflight.py'], cwd=scope, env=env, stdout=log, timeout=90)
    require(result.returncode == 0, 'Setup failed; preserve attempt and cleanup evidence')
    if setup_only:
        return {'passed': True, 'setup_only': True, 'native_gates_run': False, 'evidence': str(scope / 'preflight.json')}
    with (scope / 'native-owned.log').open('x') as log:
        result = owned_run.run([sys.executable, RECIPE / 'run-native-compat-v5.py', '--config', scope / 'native.config.json', '--evidence', scope / 'evidence'],
                               cwd=scope, env=env, stdout=log, timeout=750)
    require(result.returncode == 0, 'Native compatibility failed; preserve raw evidence')
    return {'passed': True, 'evidence': str(scope / 'evidence/report.json'), 'cleanup_required': True,
            'reports': export_reports(scope)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run', 'prepare-attempt', 'verify-attempt', 'run-attempt', 'cleanup-attempt',
        'prepare-ap', 'verify-ap', 'replay-ap', 'run-ap', 'cleanup-ap'))
    parser.add_argument('--scope', type=Path)
    parser.add_argument('--attempt', type=Path)
    parser.add_argument('--old-package', type=Path)
    parser.add_argument('--execution', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--coordinator', type=Path)
    parser.add_argument('--setup-only', action='store_true')
    args = parser.parse_args()
    if args.command == 'run':
        require(args.scope is not None, '--scope is required')
        result = run(args.scope, setup_only=args.setup_only)
    else:
        require(args.attempt is not None, '--attempt is required')
        sys.path.insert(0, str(RECIPE))
        import workflow
        if args.command == 'prepare-attempt':
            require(args.old_package is not None, '--old-package is required')
            result = workflow.prepare_attempt(args.attempt, args.old_package)
        elif args.command == 'verify-attempt':
            context = workflow.verify_attempt(args.attempt)
            result = {'verified': True, 'attempt': context['attempt']}
        elif args.command == 'cleanup-attempt':
            require(args.output is not None, '--output is required')
            result = workflow.cleanup_attempt(args.attempt, args.output)
        elif args.command == 'prepare-ap':
            require(args.profile is not None and args.coordinator is not None, '--profile and --coordinator are required')
            result = workflow.prepare_ap(args.attempt, args.coordinator, json.loads(args.profile.read_text()))
        elif args.command == 'verify-ap':
            context = workflow.verify_ap(args.attempt)[0]
            result = {'verified': True, 'native_attempt': context['native_attempt']}
        elif args.command == 'replay-ap':
            result = workflow.replay_ap(args.attempt)
        elif args.command == 'cleanup-ap':
            require(args.output is not None, '--output is required')
            result = workflow.cleanup_ap(args.attempt, args.output)
        elif args.command == 'run-ap':
            require(args.execution is not None, '--execution is required')
            result = workflow.run_ap(args.attempt, args.execution)
        else:
            require(args.execution is not None, '--execution is required')
            result = workflow.run_attempt(args.attempt, args.execution)
    print(json.dumps(result))
    if args.command in ('cleanup-attempt', 'cleanup-ap') and result['outcome'] != 'clean':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
