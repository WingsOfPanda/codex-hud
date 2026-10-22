"""Compose compatibility with the existing local executor and core receipts."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

SCRIPTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))
import release_compat as compat
import release_native as native
import release_ownership as ownership
import release_protocol as protocol


def bound_execution(directory, profile, expected):
    directory = Path(directory).resolve(strict=True)
    record = protocol.load(directory / 'state.json')
    helper = Path(profile['executor_package_root']) / 'bin/codex-plus-runtime'
    protocol.require(record['id'] == directory.name and record['state'] == 'running' and
        Path(record['package']).resolve() == Path(profile['executor_package_root']).resolve() and
        record['runtime']['sha256'] == native.sha(helper), 'Managed execution identity mismatch')
    protocol.require(all((record.get('workload') or {}).get(key) == value
                         for key, value in native.process_identity(os.getpid()).items()),
                     'Managed workload does not own this process')
    protocol.require(record['manifest']['command']['spec']['argv'] == expected,
                     'Managed command binding mismatch')
    return record['id'], native.sha(helper)


def closure(profile):
    paths = [SCRIPTS / name for name in ('release_compat.py', 'release_native.py',
                                       'release_protocol.py', 'release_ownership.py')]
    paths += [path for path in compat.RECIPE.iterdir() if path.is_file()]
    paths += [Path(profile['python']['path']), Path(profile['executor_package_root']) / 'bin/codex-plus-runtime']
    files = {str(path.resolve()): protocol.file_record(path.resolve()) for path in paths}
    if profile['python'].get('fixture_imports'):
        files.update(native.fixture_closure(profile))
    return files


def executed_origins(profile, paths):
    # Package submodules stay frozen in preparation; core admits exact import origins.
    origins = {Path(row['origin']).resolve() for row in profile['python'].get('fixture_imports', {}).values()}
    packages = [path.parent for path in origins if path.name == '__init__.py']
    return sorted(path for path in paths if Path(path).resolve() in origins or
                  not any(Path(path).resolve().is_relative_to(package) for package in packages))


def proposal(root, profile, command, inputs, *, topic):
    script = str(SCRIPTS / 'release_compat.py')
    return {'schema': 1, 'topic': topic,
        'argv': [profile['python']['path'], '-B', script, command, '--attempt', str(root), '--execution', '{execution}'],
        'cwd': profile['tooling_root'], 'env': native.python_env(profile),
        'inputs': [{'source': str(root / name), 'path': name} for name in inputs],
        'exclusive_keys': ['release-compat-' + protocol.digest(str(root))[:24]],
        'expected_duration_seconds': 900, 'deadline_seconds': 1500, 'cancel_grace_seconds': 90}


def prepare_attempt(root, old_package):
    root = Path(root).resolve(strict=True)
    metadata, plan, profile = protocol.require_native_attempt(root)
    native.verify(root)
    approval = protocol.approve_package(root, source_root=profile['source_root'])
    approval_path = root / 'outputs/compat-package-approval.json'
    protocol.write_once(approval_path, approval)
    package_report = protocol.load(protocol.relative_file(root, protocol._receipts(root)['package']['evidence']))
    package = protocol.relative_file(root, package_report['package_dir'])
    archive = protocol.relative_file(root, package_report['archive'])
    helper = Path(profile['executor_package_root']) / 'bin/codex-plus-runtime'
    settings = {'old_package': str(Path(old_package).resolve(strict=True)), 'new_package': str(package),
        'new_archive': str(archive), 'stock_binary': profile['native_build']['official_binary'],
        'qualification_evidence': str(approval_path), 'new_manifest_sha256': approval['manifest_sha256'],
        'new_archive_sha256': approval['archive_sha256'], 'qualification_evidence_sha256': native.sha(approval_path),
        'root_accepted_final_package': True}
    prepared = compat.prepare(plan, profile, root, config=settings,
        helper_binding={'helper': str(helper), 'sha256': native.sha(helper)})
    old = Path(settings['old_package'])
    old_manifest = protocol.load(old / 'codex-plus.json')
    pin = plan['previous_release']['targets'][metadata['architecture']]['manifest_sha256']
    protocol.require(native.sha(old / 'codex-plus.json') == pin and
        old_manifest['release'] == plan['previous_release']['tag'] and
        old_manifest['plus_commit'] == plan['compat']['previous_source_commit'] and
        old_manifest['target'] == profile['triple'], 'Previous package identity mismatch')
    protocol.write_once(root / 'outputs/compat-previous-package.json', {'path': str(old), 'manifest_sha256': pin})
    files = closure(profile)
    for base in (old, package):
        for path in base.rglob('*'):
            if path.is_file():
                protocol.require(not path.is_symlink(), 'Package input symlink refused')
                files[str(path.resolve())] = protocol.file_record(path)
    for name in ('stock_binary', 'new_archive'):
        path = Path(settings[name])
        files[str(path.resolve())] = protocol.file_record(path)
    for name in ('native-preparation.json', 'ownership-baseline.json', 'outputs/compat-package-approval.json',
                 'outputs/compat-previous-package.json', 'compat/native.config.json',
                 'compat/helper-binding.json', 'compat/footer-contract.json'):
        path = root / name
        files[str(path)] = protocol.file_record(path)
    context = {'attempt': metadata['attempt'], 'plan_sha256': protocol.digest(plan),
        'profile_sha256': protocol.digest(profile), 'closure': files, 'scope': prepared['scope']}
    protocol.write_once(root / 'compat-preparation.json', context)
    inputs = ['attempt.json', 'plan.json', 'profile.json', 'compat-preparation.json',
              'outputs/compat-package-approval.json', 'outputs/compat-previous-package.json',
              'compat/native.config.json', 'compat/helper-binding.json', 'compat/footer-contract.json']
    result = proposal(root, profile, 'run-attempt', inputs, topic='Qualify local compatibility captures')
    result['inputs'] += [{'source': name, 'path': 'closure/' + str(index)} for index, name in enumerate(files)
                         if Path(name).is_relative_to(SCRIPTS.parent)]
    result['external_dependencies'] = [{'identity': 'Pinned native package, source and compatibility closure',
        'precondition': {'argv': [profile['python']['path'], '-B', str(SCRIPTS / 'release_compat.py'),
                                 'verify-attempt', '--attempt', str(root)],
                         'cwd': profile['tooling_root'], 'env': native.python_env(profile), 'deadline_seconds': 180}}]
    result['cleanup'] = {'argv': [profile['python']['path'], '-B', str(SCRIPTS / 'release_compat.py'),
                                 'cleanup-attempt', '--attempt', str(root), '--output', str(root / 'outputs/compat-job-cleanup.json')],
                         'cwd': profile['tooling_root'], 'env': native.python_env(profile), 'deadline_seconds': 180}
    protocol.write_once(root / 'compat-job-proposal.json', result)
    return result


def verify_attempt(root):
    root = Path(root).resolve(strict=True)
    metadata, plan, profile = protocol.require_native_attempt(root)
    native.verify(root)
    context = protocol.load(root / 'compat-preparation.json')
    protocol.require(context['attempt'] == metadata['attempt'] and context['plan_sha256'] == protocol.digest(plan)
                     and context['profile_sha256'] == protocol.digest(profile), 'Compatibility preparation binding changed')
    protocol.require(all(protocol.file_record(path) == record for path, record in context['closure'].items()),
                     'Compatibility input closure changed')
    return context


def audit_scope(scope, *, remove_short=False):
    scope = Path(scope).resolve(strict=True)
    config = compat.validate_config(protocol.load(scope / 'native.config.json'))
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    records_path = scope / 'owned-process-identities.jsonl'
    records = [json.loads(line) for line in records_path.read_text().splitlines()] if records_path.exists() else []
    baseline_path = scope / 'unreadable-baseline.json'
    baseline = protocol.load(baseline_path) if baseline_path.exists() else {'boot_id': boot, 'processes': {}}
    roots = [scope]
    binding_path = scope / 'short-state.json'
    binding = protocol.load(binding_path) if binding_path.exists() else None
    expected = None
    if binding is not None:
        short = Path(binding['path'])
        protocol.require(short.parent.resolve() == Path(config['short_root']).resolve() and
                         len(short.name) == 2 and short.name.isalnum(), 'Short directory escapes declared namespace')
        roots.append(short)
        expected = dict(path=str(short), dev=binding['device'], ino=binding['inode'],
                        uid=binding['uid'], boot_id=binding['boot_id'])
        paired = scope / 'short-binding.json'
        if paired.exists():
            proof = protocol.load(paired)
            protocol.require(all(proof[key] == binding[key] for key in ('path', 'device', 'inode')) and
                             proof['baseline_sha256'] == native.sha(baseline_path), 'Compatibility ownership binding changed')
    result = ownership.inspect_processes(roots, baseline, scope=scope, recorded=records,
                                          completed_guards=ownership.verify_guard_journal(records, boot))
    result.update(outcome='clean' if result['passed'] else 'blocked',
                  owned_remaining=result['live_owned_processes'], unknown=result['unknown_unreadable_same_user_processes'],
                  signals_sent=0, short_removed=False, short_identity=expected)
    if remove_short and binding is not None and result['passed']:
        protocol.require(binding['boot_id'] == boot and binding['uid'] == os.getuid(), 'Short directory owner/boot changed')
        ownership._remove_socket(scope, short, expected, protocol)
        result['short_removed'] = True
    return result


def cleanup_attempt(root, output):
    root = Path(root).resolve(strict=True)
    verify_attempt(root)
    result = audit_scope(root / 'compat', remove_short=True)
    protocol.require(Path(output).resolve().is_relative_to(root), 'Cleanup output escapes attempt')
    protocol.write_once(output, result)
    return result


def cleanup_record(root, report, action):
    """Preserve a failed audit and bind an explicit unknown cleanup outcome."""
    root, report = Path(root), Path(report)
    try:
        action()
        raw = protocol.load(report)
        outcome = raw['outcome']
        counts = {name: len(raw[name]) if isinstance(raw[name], list) else raw[name]
                  for name in ('owned_remaining', 'unknown')}
    except Exception as exc:
        original = report
        report = report.with_name(report.stem + '-failure.json')
        outcome, counts = 'unknown', {'owned_remaining': 0, 'unknown': 1}
        protocol.write_once(report, dict(outcome=outcome, **counts, error=repr(exc),
            raw_report=original.relative_to(root).as_posix() if original.exists() else None))
    return dict(outcome=outcome, **counts, report=report.relative_to(root).as_posix())


def run_attempt(root, execution):
    root = Path(root).resolve(strict=True)
    metadata, plan, profile = protocol.require_native_attempt(root)
    context = verify_attempt(root)
    protocol.require(not any(name in protocol._receipts(root) for name in ('compat', 'captures')), 'Compatibility receipt already exists')
    protocol.require(not (root / 'compat-started.json').exists(), 'Interrupted compatibility has an unknown outcome')
    expected = [profile['python']['path'], '-B', str(SCRIPTS / 'release_compat.py'), 'run-attempt', '--attempt', str(root), '--execution', str(Path(execution).resolve())]
    job, executor_sha = bound_execution(execution, profile, expected)
    (root / 'logs').mkdir(mode=0o700, exist_ok=True)
    protocol.write_once(root / 'compat-started.json', {'job_id': job, 'started_ns': time.time_ns()})
    argv = [profile['python']['path'], '-B', str(SCRIPTS / 'release_compat.py'), 'run', '--scope', context['scope']]
    env = native.python_env(profile)
    code, error = None, None
    try:
        with (root / 'logs/compat-managed.log').open('xb') as log:
            code = native.owned_run(argv, helper=Path(profile['executor_package_root']) / 'bin/codex-plus-runtime',
                python=profile['python']['path'], cwd=root, env=env, stdout=log, timeout=900)
    except Exception as exc:
        error = repr(exc)
    audit = cleanup_record(root, root / 'outputs/compat-cleanup.json',
                           lambda: cleanup_attempt(root, root / 'outputs/compat-cleanup.json'))
    root_cleanup = cleanup_record(root, root / 'outputs/compat-root-cleanup.json',
                                  lambda: native.cleanup(root, root / 'outputs/compat-root-cleanup.json'))
    outcomes = (audit['outcome'], root_cleanup['outcome'])
    cleanup = {'outcome': 'clean' if outcomes == ('clean', 'clean') else 'unknown' if 'unknown' in outcomes else 'blocked',
        'owned_remaining': audit['owned_remaining'] + root_cleanup['owned_remaining'],
        'unknown': audit['unknown'] + root_cleanup['unknown'], 'report': 'outputs/compat-combined-cleanup.json'}
    protocol.write_once(root / cleanup['report'], dict(cleanup, audits=[audit, root_cleanup]))
    try:
        verify_attempt(root)
    except Exception as exc:
        error = repr(exc)
    protocol.write_once(root / 'outputs/compat-command.json', {'job_id': job, 'argv': argv,
        'started_ns': protocol.load(root / 'compat-started.json')['started_ns'], 'finished_ns': time.time_ns(),
        'exit_code': code, 'error': error})
    journal = root / 'process-journal.jsonl'
    if journal.exists():
        protocol.write_once(root / 'outputs/compat-processes.json',
                            [json.loads(line) for line in journal.read_text().splitlines()])
    execution_record = {'job_id': job, 'executor_package_root': profile['executor_package_root'],
        'executor_sha256': executor_sha, 'architecture': metadata['architecture'],
        'plus_source_commit': plan['plus_source_commit'], 'tooling_commit': plan['tooling_commit'],
        'outcome': 'passed' if code == 0 and error is None else 'failed' if code is not None else 'unknown',
        'exit_code': code, 'argv': argv, 'cwd': str(root), 'env': env}
    if code != 0 and not (root / 'compat/compat-report.json').exists():
        protocol.write_once(root / 'compat/compat-report.json', {'architecture': metadata['architecture'],
            'plus_source_commit': plan['plus_source_commit'], 'error': error or 'Compatibility command failed'})
    executed = set(closure(profile))
    for package in (protocol.load(root / 'outputs/compat-previous-package.json')['path'],
                    protocol.load(root / 'compat/native.config.json')['new_package']):
        executed.update(str(Path(package) / name) for name in ('bin/codex', 'bin/codex-plus-runtime',
            'bin/codex-code-mode-host', 'bin/codex-plus', 'codex-resources/bwrap'))
    executed.add(profile['native_build']['official_binary'])
    executed.update(str(Path(profile['source_root']) / name) for name in plan['compat']['fixture_files'])
    outputs = [path.relative_to(root).as_posix() for name in ('compat', 'outputs', 'logs')
               for path in (root / name).rglob('*') if path.is_file() and not path.is_symlink()]
    for stage in (('compat', 'captures') if code == 0 else ('compat',)):
        protocol.write_receipt(root, stage, inputs=['attempt.json', 'plan.json', 'profile.json',
            'compat-preparation.json', 'outputs/compat-package-approval.json', 'outputs/compat-previous-package.json'],
            executed=executed_origins(profile, executed), outputs=outputs, evidence='compat/' + stage + '-report.json',
            execution=execution_record, cleanup=cleanup, previous_package='outputs/compat-previous-package.json')
    protocol.require(code == 0 and cleanup['outcome'] == 'clean' and error is None, 'Compatibility failed or cleanup blocked; preserve original attempt')
    return protocol.status(root)


def dependency_files(root):
    """Record real module origins, refusing symlink escapes and directory cycles."""
    root = Path(root).resolve(strict=True)
    result, visited = {}, set()
    for directory, names, files in os.walk(root, followlinks=True):
        directory = Path(directory).resolve(strict=True)
        protocol.require(directory.is_relative_to(root), 'AP dependency origin escapes node_modules')
        info = directory.stat()
        if (info.st_dev, info.st_ino) in visited:
            names[:] = []
            continue
        visited.add((info.st_dev, info.st_ino))
        for name in files:
            path = (directory / name).resolve(strict=True)
            protocol.require(path.is_relative_to(root), 'AP dependency origin escapes node_modules')
            result[str(path)] = protocol.file_record(path)
    return result


def ap_profile(profile, plan):
    protocol.validate_profile(profile, profile['architecture'])
    protocol.require('ap_replay' in plan['compat'] and 'ap_replay' in profile,
                     'AP replay requires explicit plan pins and replay profile paths')
    protocol.require(Path(profile['tooling_root']).resolve() == SCRIPTS.parent,
                     'Run AP replay from the pinned tooling checkout')
    protocol.require(profile['architecture'] == platform.machine() and
                     profile['hostname'] == platform.node(), 'AP replay host profile mismatch')
    protocol.require(Path(sys.executable).resolve() == Path(profile['python']['path']).resolve() and
                     sys.version.split()[0] == profile['python']['version'], 'Use the pinned AP replay Python')
    libraries = {str(Path(line.split()[-1]).resolve()) for line in Path('/proc/self/maps').read_text().splitlines()
                 if 'libpython' in line and line.split()[-1].startswith('/')}
    expected = profile['python']['libpython']
    protocol.require(libraries == ({str(Path(expected).resolve())} if expected else set()), 'AP replay Python loader mismatch')
    pins, paths = plan['compat']['ap_replay'], profile['ap_replay']
    source = Path(profile['source_root']).resolve(strict=True)
    seal = native.checkout_identity(source)
    tooling = native.checkout_identity(profile['tooling_root'])
    protocol.require(seal['head'] == pins['source_commit'] and tooling['head'] == plan['tooling_commit'], 'AP/tooling checkout pin mismatch')
    protocol.require(protocol.load(source / 'package.json')['version'] == plan['compat']['ap_version'], 'AP source version mismatch')
    installed = Path(paths['installed_bundle']).resolve(strict=True)
    protocol.require(native.sha(source / 'dist/ap.cjs') == native.sha(installed) == pins['bundle_sha256'], 'Released AP bundle differs')
    registry = protocol.load(paths['installed_registry'])
    matches = [row for rows in registry['plugins'].values() for row in rows
               if Path(row['installPath']).resolve() == installed.parent.parent]
    protocol.require(len(matches) == 1 and matches[0]['version'] == plan['compat']['ap_version'] and
                     matches[0]['gitCommitSha'] == pins['installed_plugin_commit'], 'Installed AP registry pin mismatch')
    modules = Path(paths['node_modules']).resolve(strict=True)
    protocol.require((source / 'node_modules').resolve(strict=True) == modules, 'AP module origin differs from replay profile')
    node = Path(paths['node']).resolve(strict=True)
    protocol.require(profile.get('system_tools', {}).get(str(node)) == native.sha(node), 'AP Node executable needs exact system pin')
    protocol.require((modules / 'vitest/vitest.mjs').is_file() and (modules / 'vitest/dist/index.js').is_file(),
                     'Pinned AP Vitest dependencies are unavailable; owner must provide them')
    import _json
    return {'source': seal, 'tooling': tooling,
        'python_runtime': {'interpreter': protocol.file_record(Path(sys.executable).resolve()),
            'libpython': protocol.file_record(Path(expected).resolve()) if expected else None,
            'json_extension': {'path': str(Path(_json.__file__).resolve()),
                               **protocol.file_record(Path(_json.__file__).resolve())}}}


def prepare_ap(native_root, coordinator, profile):
    native_root = Path(native_root).resolve(strict=True)
    coordinator = Path(coordinator).resolve()
    protocol.require(not coordinator.is_relative_to(native_root), 'AP evidence must remain outside the native import')
    metadata, plan, _ = protocol.read_attempt(native_root)
    state = protocol.status(native_root)
    protocol.require(not state['refused'] and all(name in state['receipts'] for name in ('package', 'native-tests', 'compat', 'captures')),
                     'AP replay requires passing immutable native package/compatibility/capture evidence')
    seals = ap_profile(profile, plan)
    capture_report = protocol.load(protocol.relative_file(native_root, protocol._receipts(native_root)['captures']['evidence']))
    inventory = protocol.load(protocol.relative_file(native_root, capture_report['capture_manifest']))
    protocol.require(native.sha(protocol.relative_file(native_root, capture_report['capture_manifest'])) == capture_report['capture_manifest_sha256'],
                     'Native capture inventory changed')
    protocol._verify_records(native_root, inventory['files'], {}, protocol._transport_manifest(native_root))
    capture = native_root / 'compat/evidence/cursor-v2'
    manifest = protocol.load(capture / 'manifest.json')
    integrity = protocol.load(capture / 'integrity.json')
    binding = protocol.load(capture / 'package-binding.json')
    protocol.require(manifest['candidate_acceptance'] and manifest['capture_complete'] and len(manifest['captures']) == 20 and integrity['passed'], 'Incomplete native AP input')
    protocol.require(binding['accepted'] and binding['native'] and binding['source'] == plan['plus_source_commit'] and
                     binding['archive_sha256'] == capture_report['package_sha256'] and
                     binding['capture_manifest_sha256'] == native.sha(capture / 'manifest.json') and
                     binding['integrity_sha256'] == native.sha(capture / 'integrity.json'), 'Native AP package/capture binding differs')
    files = closure(profile) | dependency_files(profile['ap_replay']['node_modules'])
    source = Path(profile['source_root'])
    files.update({str((source / name).resolve()): protocol.file_record(source / name)
                  for name, row in seals['source']['files'].items() if 'link' not in row})
    for name in ('node', 'installed_bundle', 'installed_registry'):
        path = Path(profile['ap_replay'][name]).resolve(strict=True)
        files[str(path)] = protocol.file_record(path)
    coordinator.mkdir(mode=0o700)
    protocol.write_once(coordinator / 'ap-profile.json', profile)
    protocol.write_once(coordinator / 'native-plan.json', plan)
    protocol.write_once(coordinator / 'native-captures.json', capture_report)
    protocol.write_once(coordinator / 'native-capture-inventory.json', inventory)
    data = coordinator / 'captures'
    data.mkdir(mode=0o700)
    for record in manifest['captures']:
        protocol.require(record['name'] == Path(record['name']).name, 'Unsafe capture state')
        directory = data / record['name']
        directory.mkdir(mode=0o700)
        for filename, key in (('screen.txt', 'screen_sha256'), ('look.txt', 'look_sha256')):
            path = capture / record['name'] / filename
            protocol.require(native.sha(path) == record[key], 'Native cursor pair changed')
            shutil.copyfile(path, directory / filename)
    protocol.write_once(data / 'ap-replay-index.json', {'bundle_version': 2,
        'fixtures': [{'id': row['name'], 'expected': row['expected']} for row in manifest['captures']]})
    context = {'native_root': str(native_root), 'native_attempt': metadata['attempt'],
        'plan_sha256': protocol.digest(plan), 'profile_sha256': protocol.digest(profile), 'seals': seals, 'closure': files,
        'capture_manifest_sha256': capture_report['capture_manifest_sha256'], 'package_sha256': capture_report['package_sha256'],
        'capture_data': {path.relative_to(coordinator).as_posix(): protocol.file_record(path) for path in data.rglob('*') if path.is_file()}}
    protocol.write_once(coordinator / 'ownership-baseline.json', ownership.capture_baseline([coordinator], scope=coordinator))
    context['ownership_baseline_sha256'] = native.sha(coordinator / 'ownership-baseline.json')
    protocol.write_once(coordinator / 'ap-preparation.json', context)
    inputs = ['ap-profile.json', 'native-plan.json', 'native-captures.json', 'native-capture-inventory.json',
              'ap-preparation.json', 'ownership-baseline.json', *context['capture_data']]
    result = proposal(coordinator, profile, 'run-ap', inputs, topic='Replay pinned AP captures')
    result.update(expected_duration_seconds=240, deadline_seconds=600)
    result['external_dependencies'] = [{'identity': 'Released AP bundle, clean source and exact module origins',
        'precondition': {'argv': [profile['python']['path'], '-B', str(SCRIPTS / 'release_compat.py'), 'verify-ap', '--attempt', str(coordinator)],
                         'cwd': profile['tooling_root'], 'env': native.python_env(profile), 'deadline_seconds': 180}}]
    result['cleanup'] = {'argv': [profile['python']['path'], '-B', str(SCRIPTS / 'release_compat.py'),
        'cleanup-ap', '--attempt', str(coordinator), '--output', str(coordinator / 'job-cleanup.json')],
        'cwd': profile['tooling_root'], 'env': native.python_env(profile), 'deadline_seconds': 180}
    protocol.write_once(coordinator / 'ap-job-proposal.json', result)
    return result


def verify_ap(root):
    root = Path(root).resolve(strict=True)
    context, plan, profile = (protocol.load(root / name) for name in ('ap-preparation.json', 'native-plan.json', 'ap-profile.json'))
    protocol.require(context['plan_sha256'] == protocol.digest(plan) and context['profile_sha256'] == protocol.digest(profile), 'AP preparation binding changed')
    protocol.require(context['ownership_baseline_sha256'] == native.sha(root / 'ownership-baseline.json'), 'AP ownership baseline changed')
    protocol.require(ap_profile(profile, plan) == context['seals'], 'AP source/tooling seal changed')
    modules = Path(profile['ap_replay']['node_modules']).resolve(strict=True)
    expected = {path: record for path, record in context['closure'].items() if Path(path).is_relative_to(modules)}
    protocol.require(dependency_files(modules) == expected, 'AP dependency closure changed')
    protocol.require(all(protocol.file_record(path) == record for path, record in context['closure'].items()
                         if not Path(path).is_relative_to(modules)), 'AP execution closure changed')
    protocol.require(all(protocol.file_record(protocol.relative_file(root, name)) == record for name, record in context['capture_data'].items()), 'AP capture data changed')
    return context, plan, profile


def replay_ap(root):
    root = Path(root).resolve(strict=True)
    context, plan, profile = verify_ap(root)
    source = Path(profile['source_root'])
    node = Path(profile['ap_replay']['node'])
    modules = Path(profile['ap_replay']['node_modules'])
    env = native.python_env(profile) | {'AP_REPLAY_SOURCE': str(source), 'AP_REPLAY_NODE_MODULES': str(modules),
        'AP_REPLAY_RECIPE': str(compat.RECIPE), 'AP_REPLAY_SCOPE': str(root),
        'AP_CAPTURE_INDEX': str(root / 'captures/ap-replay-index.json'), 'AP_HOME': str(root / 'ap-home'),
        'AP_FORENSICS_BACKEND': 'queue', 'CODEX_HOME': str(root / 'codex-home'),
        'CODEX_PLUS_RELEASE_QA_SCOPE': str(root), 'AP_REPLAY_OBSERVER_OUTPUT': str(root / 'observed-classifier.json')}
    (root / 'ap-home').mkdir(mode=0o700)
    commands = []
    for label, test, count in (('release', source / 'tests/codex-plus-footer.test.ts', 22),
                              ('observer', compat.RECIPE / 'ap-release-replay-observer.test.ts', 1)):
        argv = [str(node), str(modules / 'vitest/vitest.mjs'), 'run', str(test), '--config', str(compat.RECIPE / 'vitest.replay.config.ts'),
                '--reporter', 'json', '--outputFile', str(root / (label + '-vitest.json'))]
        with (root / (label + '.log')).open('xb') as log:
            result = subprocess.run(argv, cwd=source, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=90)
        commands.append({'argv': argv, 'exit_code': result.returncode})
        tests = protocol.load(root / (label + '-vitest.json'))
        protocol.require(result.returncode == 0 and tests['success'] and tests['numPassedTests'] == count and
            tests['numFailedTests'] == tests['numPendingTests'] == tests['numTodoTests'] == 0, 'Pinned AP replay test failed')
    observed = protocol.load(root / 'observed-classifier.json')
    protocol.require(observed['passed'] and len(observed['captures']) == 20 and len(observed['busy_pairs']) == 2, 'AP observer evidence incomplete')
    verify_ap(root)
    protocol.write_once(root / 'replay-report.json', {'passed': True, 'candidate_acceptance': True,
        'ap_version': plan['compat']['ap_version'], **plan['compat']['ap_replay'], 'commands': commands,
        'captures': 20, 'busy_pairs': 2, 'tests': 23, 'live_nudge_invoked': False,
        'classifier_sha256': native.sha(source / 'src/commands/nudge.ts'),
        'cursor_sha256': native.sha(source / 'src/core/tmux.ts'),
        'observer_sha256': native.sha(compat.RECIPE / 'ap-release-replay-observer.test.ts')})
    return {'passed': True, 'tests': 23, 'captures': 20, 'busy_pairs': 2}


def cleanup_ap(root, output):
    root = Path(root).resolve(strict=True)
    verify_ap(root)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    journal = root / 'process-journal.jsonl'
    rows = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    result = ownership.inspect_processes([root], protocol.load(root / 'ownership-baseline.json'), scope=root,
        recorded=rows, completed_guards=ownership.verify_guard_journal(rows, boot))
    result.update(outcome='clean' if result['passed'] else 'blocked', owned_remaining=result['live_owned_processes'],
                  unknown=result['unknown_unreadable_same_user_processes'], signals_sent=0)
    protocol.require(Path(output).resolve().is_relative_to(root), 'AP cleanup output escapes coordinator')
    protocol.write_once(output, result)
    return result


def run_ap(root, execution):
    root = Path(root).resolve(strict=True)
    context, plan, profile = verify_ap(root)
    protocol.require(not (root / 'ap-started.json').exists() and not (root / 'ap-receipt.json').exists(),
                     'AP execution already started; preserve its original outcome')
    expected = [profile['python']['path'], '-B', str(SCRIPTS / 'release_compat.py'), 'run-ap', '--attempt', str(root), '--execution', str(Path(execution).resolve())]
    job, executor_sha = bound_execution(execution, profile, expected)
    protocol.write_once(root / 'ap-started.json', {'job_id': job, 'started_ns': time.time_ns()})
    argv = [profile['python']['path'], '-B', str(SCRIPTS / 'release_compat.py'), 'replay-ap', '--attempt', str(root)]
    env = native.python_env(profile) | {'CODEX_PLUS_RELEASE_QA_SCOPE': str(root)}
    code, error = None, None
    try:
        with (root / 'managed.log').open('xb') as log:
            code = native.owned_run(argv, helper=Path(profile['executor_package_root']) / 'bin/codex-plus-runtime',
                python=profile['python']['path'], cwd=root, env=env, stdout=log, timeout=300)
    except Exception as exc:
        error = repr(exc)
    audit = cleanup_record(root, root / 'cleanup.json', lambda: cleanup_ap(root, root / 'cleanup.json'))
    protocol.write_once(root / 'ap-command.json', {'job_id': job, 'argv': argv,
        'started_ns': protocol.load(root / 'ap-started.json')['started_ns'], 'finished_ns': time.time_ns(),
        'exit_code': code, 'error': error})
    metadata, _, _ = protocol.read_attempt(context['native_root'])
    raw = {str(path.relative_to(root)): native.sha(path) for path in
           (root / 'replay-report.json', root / 'release-vitest.json', root / 'observer-vitest.json', root / 'observed-classifier.json') if path.is_file()}
    report = {'architecture': metadata['architecture'], 'plus_source_commit': plan['plus_source_commit'],
        'ap_version': plan['compat']['ap_version'], 'package_sha256': context['package_sha256'],
        'ap_replay': plan['compat']['ap_replay'],
        'capture_manifest_sha256': context['capture_manifest_sha256'],
        'checks': {name: {'outcome': 'passed' if code == 0 else 'failed', 'evidence': raw}
                   for name in plan['gates'][metadata['architecture']]['ap']['checks']}}
    protocol.write_once(root / 'ap-report.json', report)
    execution_record = {'job_id': job, 'executor_package_root': profile['executor_package_root'], 'executor_sha256': executor_sha,
        'architecture': profile['architecture'], 'plus_source_commit': plan['plus_source_commit'], 'tooling_commit': plan['tooling_commit'],
        'outcome': 'passed' if code == 0 and error is None else 'failed' if code is not None else 'unknown', 'exit_code': code,
        'argv': argv, 'cwd': str(root), 'env': env, 'replay_profile_sha256': protocol.digest(profile)}
    inputs = ['ap-preparation.json', 'ap-profile.json', 'native-plan.json', 'native-captures.json', 'native-capture-inventory.json', 'ownership-baseline.json']
    outputs = [path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file() and not path.is_symlink()]
    protocol.write_ap_receipt(context['native_root'], root, inputs=inputs,
        executed=executed_origins(profile, [path for path in context['closure']
            if Path(path) != Path(profile['ap_replay']['installed_registry']).resolve()]),
        outputs=outputs, evidence='ap-report.json', execution=execution_record, cleanup=audit,
        replay_profile=profile)
    protocol.require(code == 0 and audit['outcome'] == 'clean' and error is None, 'AP replay failed or cleanup blocked; preserve coordinator evidence')
    return report
