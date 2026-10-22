"""Immutable, local release evidence. This module never starts a process.

Receipts establish consistency of supplied native evidence, not host attestation.
Transport must supply the export digest through the owning session separately.
"""
import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import string
import sys
import tarfile
import tempfile
import tomllib
from datetime import datetime, timezone


GATES = ('package', 'native-tests', 'compat', 'captures', 'ap')
STAGES = ('preflight', 'frontend', 'runtime') + GATES
TARGETS = {'x86_64': 'x86_64-unknown-linux-gnu', 'aarch64': 'aarch64-unknown-linux-gnu'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def keys(value, required, optional=()):
    require(isinstance(value, dict), 'Expected object')
    require(set(required) <= value.keys() <= set(required) | set(optional),
            f'Unexpected or missing fields: {sorted(value)}; required {sorted(required)}')


def hex_digest(value, length=64):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value), 'Invalid digest')


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_record(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), f'Not a regular file: {path}')
    info = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return {'sha256': h.hexdigest(), 'size': info.st_size, 'mode': f'{stat.S_IMODE(info.st_mode):04o}'}


def load(path):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result, f'Duplicate JSON field: {name}')
            result[name] = value
        return result
    with Path(path).open() as stream:
        return json.load(stream, object_pairs_hook=unique)


def write_once(path, value):
    """Exclusive creation preserves every original record, including failures."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o400)
            os.link(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink()


def relative_file(root, name):
    path = Path(name)
    require(not path.is_absolute() and path.parts and '..' not in path.parts, 'Unsafe relative evidence path')
    result = Path(root) / path
    require(result.resolve().is_relative_to(Path(root).resolve()), 'Evidence escapes attempt')
    require(not any(parent.is_symlink() for parent in [result, *result.parents]
                    if parent.is_relative_to(Path(root))), 'Symlink in evidence path')
    return result


def _constants(source_root):
    result = {}
    for node in ast.parse((Path(source_root) / 'scripts/package-release.py').read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ('UPSTREAM_VERSION', 'PLUS_VERSION', 'UPSTREAM_COMMIT', 'PATCH'):
                result[node.targets[0].id] = ast.literal_eval(node.value)
    return result


def validate_plan(plan, source_root):
    keys(plan, ('schema_version', 'release', 'plus_version', 'upstream', 'plus_source_commit',
                'tooling_commit', 'patch', 'previous_release', 'targets', 'gates', 'compat',
                'rollout', 'retention_floor', 'note'))
    require(plan['schema_version'] == 1, 'Unsupported plan schema')
    keys(plan['upstream'], ('version', 'tag', 'commit'))
    for name in ('plus_source_commit', 'tooling_commit'):
        hex_digest(plan[name], 40)
    hex_digest(plan['upstream']['commit'], 40)
    constants = _constants(source_root)
    require(plan['plus_version'] == constants['PLUS_VERSION'] and
            plan['upstream'] == {'version': constants['UPSTREAM_VERSION'],
                                 'tag': 'rust-v' + constants['UPSTREAM_VERSION'],
                                 'commit': constants['UPSTREAM_COMMIT']}, 'Source/package version mismatch')
    require(plan['release'] == plan['upstream']['version'] + '-plus.' + plan['plus_version'],
            'Release version mismatch')
    cargo = tomllib.loads((Path(source_root) / 'rust-runtime/Cargo.toml').read_text())
    require(cargo['package']['version'] == plan['plus_version'], 'Cargo version mismatch')
    lock = tomllib.loads((Path(source_root) / 'rust-runtime/Cargo.lock').read_text())
    runtime = [row for row in lock['package'] if row['name'] == 'codex-plus-runtime']
    require(len(runtime) == 1 and runtime[0]['version'] == plan['plus_version'], 'Cargo lock version mismatch')
    keys(plan['patch'], ('path', 'sha256'))
    require(plan['patch']['path'] == constants['PATCH'], 'Unsupported frontend patch')
    require(file_record(relative_file(source_root, plan['patch']['path']))['sha256'] == plan['patch']['sha256'],
            'Frontend patch mismatch')
    keys(plan['previous_release'], ('tag', 'handoff_sha256', 'targets'))
    hex_digest(plan['previous_release']['handoff_sha256'])
    require(plan['previous_release']['tag'] != plan['release'], 'Previous release equals candidate')
    require(set(plan['targets']) == set(TARGETS) == set(plan['gates']), 'Both native targets are required')
    require(set(plan['previous_release']['targets']) == set(TARGETS), 'Missing rollback packages')
    for arch, triple in TARGETS.items():
        target = plan['targets'][arch]
        keys(target, ('triple', 'profile_sha256', 'build_glibc'))
        require(target['triple'] == triple and re.fullmatch(r'\d+\.\d+', target['build_glibc']), 'Invalid target')
        hex_digest(target['profile_sha256'])
        previous = plan['previous_release']['targets'][arch]
        keys(previous, ('asset', 'sha256', 'manifest_sha256', 'tree_sha256'))
        require(Path(previous['asset']).name == previous['asset'], 'Unsafe previous asset')
        for name in ('sha256', 'manifest_sha256', 'tree_sha256'):
            hex_digest(previous[name])
        keys(plan['gates'][arch], GATES)
        for gate in GATES:
            spec = plan['gates'][arch][gate]
            if gate == 'native-tests':
                keys(spec, ('suites',))
                require(bool(spec['suites']), 'Native suites required')
                for suite in spec['suites'].values():
                    keys(suite, ('passed', 'allowed_ignored'))
                    require(type(suite['passed']) is int and suite['passed'] > 0 and
                            isinstance(suite['allowed_ignored'], dict), 'Invalid native suite requirements')
            else:
                keys(spec, ('checks',))
                require(isinstance(spec['checks'], list) and bool(spec['checks']) and
                        all(isinstance(x, str) and x for x in spec['checks']) and
                        len(spec['checks']) == len(set(spec['checks'])), 'Explicit unique checks required')
    keys(plan['compat'], ('ap_version', 'fixture_ssh_destinations', 'recipe_revision',
                          'previous_source_commit', 'stock_binary_sha256', 'fixture_files'), ('ap_replay',))
    require(isinstance(plan['compat']['ap_version'], str) and plan['compat']['ap_version'] and
            isinstance(plan['compat']['fixture_ssh_destinations'], list) and
            all(isinstance(host, str) and host for host in plan['compat']['fixture_ssh_destinations']),
            'Invalid compatibility requirements')
    require(type(plan['compat']['recipe_revision']) is int and plan['compat']['recipe_revision'] == 1,
            'Unsupported compatibility recipe')
    hex_digest(plan['compat']['previous_source_commit'], 40)
    require(set(plan['compat']['stock_binary_sha256']) == set(TARGETS), 'Missing official binary pins')
    for sha in plan['compat']['stock_binary_sha256'].values():
        hex_digest(sha)
    require(isinstance(plan['compat']['fixture_files'], dict) and bool(plan['compat']['fixture_files']),
            'Compatibility fixture closure required')
    for name, sha in plan['compat']['fixture_files'].items():
        require(isinstance(name, str) and not Path(name).is_absolute() and '..' not in Path(name).parts,
                'Invalid compatibility fixture path')
        hex_digest(sha)
    if 'ap_replay' in plan['compat']:
        replay = plan['compat']['ap_replay']
        keys(replay, ('source_commit', 'installed_plugin_commit', 'bundle_sha256'))
        for name in ('source_commit', 'installed_plugin_commit'):
            hex_digest(replay[name], 40)
        hex_digest(replay['bundle_sha256'])
    keys(plan['rollout'], ('daemon_restart', 'quiet_checkpoint', 'executions', 'session_restart',
                           'agents_md', 'retain', 'notes'))
    rollout = plan['rollout']
    require(type(rollout['daemon_restart']) is bool and type(rollout['quiet_checkpoint']) is bool and
            rollout['executions'] in ('wait', 'continue') and
            rollout['session_restart'] in ('none', 'optional', 'required') and
            (rollout['agents_md'] is None or isinstance(rollout['agents_md'], str)) and
            isinstance(rollout['notes'], str), 'Invalid rollout')
    require(isinstance(rollout['retain'], list) and plan['retention_floor'] in rollout['retain'] and
            plan['previous_release']['tag'] in rollout['retain'], 'Rollback retention missing')
    require(isinstance(plan['note'], str) and plan['note'].startswith('docs/releases/') and
            '..' not in Path(plan['note']).parts, 'Invalid companion note')
    return plan


def validate_profile(profile, arch, observed=None):
    keys(profile, ('schema_version', 'host_id', 'hostname', 'architecture', 'triple', 'build_root',
                   'short_root', 'tooling_root', 'source_root', 'toolchain', 'python', 'executor_package_root'),
         ('native_build', 'system_tools', 'ap_replay'))
    require(profile['schema_version'] == 1 and profile['architecture'] == arch and
            profile['triple'] == TARGETS.get(arch), 'Wrong native profile architecture')
    require(all(isinstance(profile[name], str) and profile[name] for name in ('host_id', 'hostname')),
            'Missing native host identity')
    for name in ('build_root', 'short_root', 'tooling_root', 'source_root', 'executor_package_root'):
        require(isinstance(profile[name], str) and Path(profile[name]).is_absolute() and
                '..' not in Path(profile[name]).parts, f'Invalid host path: {name}')
    keys(profile['toolchain'], ('rustc', 'cargo', 'version'))
    keys(profile['python'], ('path', 'version', 'libpython'), ('fixture_sites', 'fixture_imports'))
    for path in (profile['toolchain']['rustc'], profile['toolchain']['cargo'], profile['python']['path']):
        require(isinstance(path, str) and Path(path).is_absolute(), 'Tool must have an absolute pin')
    require(profile['python']['libpython'] is None or Path(profile['python']['libpython']).is_absolute(),
            'Invalid libpython pin')
    if 'fixture_sites' in profile['python']:
        sites = profile['python']['fixture_sites']
        require(isinstance(sites, list) and len(sites) == len(set(sites)) and
                all(isinstance(path, str) and Path(path).is_absolute() and '..' not in Path(path).parts
                    for path in sites), 'Invalid fixture site paths')
    if 'fixture_imports' in profile['python']:
        imports = profile['python']['fixture_imports']
        require(isinstance(imports, dict), 'Invalid fixture import pins')
        for name, pin in imports.items():
            require(isinstance(name, str) and re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', name),
                    'Invalid fixture module name')
            keys(pin, ('origin', 'sha256'))
            require(isinstance(pin['origin'], str) and Path(pin['origin']).is_absolute() and
                    '..' not in Path(pin['origin']).parts, 'Invalid fixture module origin')
            hex_digest(pin['sha256'])
    if 'native_build' in profile:
        native = profile['native_build']
        paths = ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache', 'license_cache', 'official_binary')
        keys(native, (*paths, 'cargo_jobs', 'test_threads', 'official_sha256'))
        for name in paths:
            require(isinstance(native[name], str) and Path(native[name]).is_absolute() and
                    '..' not in Path(native[name]).parts, f'Invalid native build path: {name}')
        for name in ('cargo_jobs', 'test_threads'):
            require(type(native[name]) is int and native[name] > 0, f'Invalid native limit: {name}')
        hex_digest(native['official_sha256'])
    if 'system_tools' in profile:
        require(isinstance(profile['system_tools'], dict), 'Invalid system tool pins')
        for name, sha in profile['system_tools'].items():
            require(isinstance(name, str) and Path(name).is_absolute() and '..' not in Path(name).parts,
                    'Invalid system tool path')
            hex_digest(sha)
    if 'ap_replay' in profile:
        replay = profile['ap_replay']
        keys(replay, ('node', 'node_modules', 'installed_bundle', 'installed_registry'))
        for path in replay.values():
            require(isinstance(path, str) and Path(path).is_absolute() and '..' not in Path(path).parts,
                    'Invalid AP replay host path')
        require(replay['node'] in profile.get('system_tools', {}), 'AP Node executable needs an exact system tool pin')
    if observed is not None:
        keys(observed, ('hostname', 'architecture', 'toolchain', 'python', 'executor_package_root',
                        'tooling_commit', 'plus_source_commit'))
        for name in ('hostname', 'architecture', 'toolchain', 'python', 'executor_package_root'):
            require(profile[name] == observed[name], f'Native host mismatch: {name}')
    return profile


def init_attempt(plan, profile, source_root, *, observed, rehearsal=False):
    validate_plan(plan, source_root)
    arch = profile['architecture']
    validate_profile(profile, arch, observed)
    require(digest(profile) == plan['targets'][arch]['profile_sha256'], 'Profile digest mismatch')
    for name in ('tooling_commit', 'plus_source_commit'):
        require(observed[name] == plan[name], f'Checkout identity mismatch: {name}')
    require(type(rehearsal) is bool, 'Invalid rehearsal label')
    token = secrets.token_hex(8)
    attempt_id = f'{plan["release"]}-{arch}-{token}'
    root = Path(profile['build_root']) / 'attempts' / attempt_id
    root.mkdir(parents=True, mode=0o700)
    for name in ('outputs', 'receipts'):
        (root / name).mkdir(mode=0o700)
    metadata = {'schema_version': 1, 'attempt': attempt_id, 'architecture': arch,
                'plan_sha256': digest(plan), 'profile_sha256': digest(profile),
                'created_at': datetime.now(timezone.utc).isoformat(), 'rehearsal': rehearsal,
                'native': observed}
    write_once(root / 'plan.json', plan)
    write_once(root / 'profile.json', profile)
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    last_intent = None
    try:
        short_root = Path(profile['short_root'])
        short_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = None
        for sequence in range(64):
            candidate = short_root / ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(2))
            last_intent = f'socket-creation-intents/{sequence:02d}.json'
            write_once(root / last_intent, {'attempt': attempt_id, 'path': str(candidate),
                                          'boot_id': boot_id, 'uid': os.getuid()})
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                continue
            info = candidate.lstat()
            identity = {'path': str(candidate), 'boot_id': boot_id, 'dev': info.st_dev,
                        'ino': info.st_ino, 'uid': info.st_uid}
            write_once(root / f'socket-creation-results/{sequence:02d}.json', identity)
            break
        require(identity is not None, 'Private socket namespace is full')
        metadata.update(socket_root=identity['path'], socket_identity=identity)
        write_once(root / 'attempt.json', metadata)
    except Exception as error:
        write_once(root / 'initialization-failure.json', dict(metadata, error=repr(error),
                    last_intent=last_intent, boot_id=boot_id, uid=os.getuid()))
        raise
    return root


def read_attempt(root):
    root = Path(root)
    metadata, plan, profile = (load(root / name) for name in ('attempt.json', 'plan.json', 'profile.json'))
    keys(metadata, ('schema_version', 'attempt', 'architecture', 'plan_sha256', 'profile_sha256',
                    'created_at', 'rehearsal', 'native', 'socket_root', 'socket_identity'))
    require(metadata['schema_version'] == 1 and type(metadata['rehearsal']) is bool, 'Invalid attempt metadata')
    require(metadata['plan_sha256'] == digest(plan) and metadata['profile_sha256'] == digest(profile),
            'Attempt input tampering')
    require(metadata['architecture'] == profile['architecture'], 'Attempt architecture mismatch')
    identity = metadata['socket_identity']
    keys(identity, ('path', 'boot_id', 'dev', 'ino', 'uid'))
    require(identity['path'] == metadata['socket_root'] and
            Path(identity['path']).parent == Path(profile['short_root']) and
            re.fullmatch(r'[A-Za-z0-9]{2}', Path(identity['path']).name) and
            isinstance(identity['boot_id'], str) and
            re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', identity['boot_id']) and
            all(type(identity[name]) is int and identity[name] >= 0 for name in ('dev', 'ino', 'uid')),
            'Invalid socket directory identity')
    validate_profile(profile, metadata['architecture'], metadata['native'])
    require(digest(profile) == plan['targets'][metadata['architecture']]['profile_sha256'], 'Profile binding mismatch')
    for name in ('tooling_commit', 'plus_source_commit'):
        require(metadata['native'][name] == plan[name], 'Attempt source identity mismatch')
    return metadata, plan, profile


def require_native_attempt(root):
    require(not (Path(root) / 'evidence-export.json').exists(), 'Imported evidence cannot run or resume')
    return read_attempt(root)


def _inventory(root, names):
    require(isinstance(names, list) and bool(names) and len(names) == len(set(names)), 'Evidence files required')
    return {name: file_record(relative_file(root, name)) for name in sorted(names)}


def _executed(profile, paths, *, root=None, prior=None, compiler_artifacts=None, plan=None, previous_package=None):
    roots = [Path(profile[name]).resolve() for name in ('tooling_root', 'source_root', 'executor_package_root')]
    tools = {Path(profile['toolchain'][name]).resolve() for name in ('rustc', 'cargo')}
    tools.add(Path(profile['python']['path']).resolve())
    tools.update(Path(name).resolve() for name in profile.get('system_tools', {}))
    fixture_pins = {Path(pin['origin']).resolve(): pin['sha256']
                    for pin in profile['python'].get('fixture_imports', {}).values()}
    tools.update(fixture_pins)
    if 'ap_replay' in profile:
        roots.append(Path(profile['ap_replay']['node_modules']).resolve())
        tools.add(Path(profile['ap_replay']['installed_bundle']).resolve())
    if 'native_build' in profile:
        tools.add(Path(profile['native_build']['official_binary']).resolve())
    require(bool(paths), 'Executed file closure required')
    result = {}
    previous_root, previous_files = None, {}
    if previous_package is not None:
        keys(previous_package, ('path', 'manifest_sha256'))
        previous_root = Path(previous_package['path'])
        require(previous_root.is_absolute() and previous_root == previous_root.resolve(), 'Previous package needs a canonical path')
        expected = plan['previous_release']['targets'][profile['architecture']]
        manifest_path = previous_root / 'codex-plus.json'
        require(previous_package['manifest_sha256'] == expected['manifest_sha256'] == file_record(manifest_path)['sha256'],
                'Previous package manifest mismatch')
        previous = load(manifest_path)
        require(previous['release'] == plan['previous_release']['tag'] and previous['target'] == profile['triple'] and
                previous['plus_commit'] == plan['compat']['previous_source_commit'] and
                previous['tree_sha256'] == expected['tree_sha256'], 'Previous package identity mismatch')
        previous_files = previous['files']
    for name in paths:
        path = Path(name)
        record = file_record(path)
        if previous_root is not None and path.is_relative_to(previous_root):
            relative = path.relative_to(previous_root).as_posix()
            relative_file(previous_root, relative)
            require(previous_files.get(relative) == record, 'Previous executable differs from qualified inventory')
            result[name] = record
            continue
        if root is not None and path.resolve().is_relative_to(Path(root).resolve()):
            relative = path.resolve().relative_to(Path(root).resolve()).as_posix()
            relative_file(root, relative)
            if path.name == 'codex-plus' and path.parent.name == 'bin':
                manifest = load(path.parent.parent / 'codex-plus.json')
                source_record = file_record(Path(profile['source_root']) / 'bin/codex-plus')
                require(all(record[key] == source_record[key] for key in ('sha256', 'size')) and
                        record['mode'] == '0755' and
                        manifest.get('release') == plan['release'] and
                        manifest.get('plus_commit') == plan['plus_source_commit'] and
                        manifest.get('target') == profile['triple'] and
                        manifest.get('files', {}).get('bin/codex-plus') == record,
                        'Package launcher is not bound to pinned source/manifest')
                result[name] = record
                continue
            with path.open('rb') as stream:
                header = stream.read(20)
            require(header[:6] == b'\x7fELF\x02\x01' and
                    int.from_bytes(header[18:20], 'little') == {'x86_64': 62, 'aarch64': 183}[profile['architecture']],
                    'Attempt executables must be native ELF artifacts')
            known = [value for row in (prior or {}).values() for value in row['outputs'].values()]
            if compiler_artifacts is not None:
                proof = compiler_artifacts
                keys(proof, ('architecture', 'plus_source_commit', 'tooling_commit', 'target',
                             'source_sha256', 'compiler_sha256', 'files'), ('cargo_artifact',))
                expected_source = digest({name: plan[name] for name in ('upstream', 'patch', 'plus_source_commit')})
                require(proof['architecture'] == profile['architecture'] and proof['target'] == profile['triple'] and
                        proof['plus_source_commit'] == plan['plus_source_commit'] and
                        proof['tooling_commit'] == plan['tooling_commit'] and proof['source_sha256'] == expected_source and
                        proof['compiler_sha256'] == file_record(profile['toolchain']['rustc'])['sha256'],
                        'Compiler artifact source/target mismatch')
                if relative in proof['files']:
                    known.append(proof['files'][relative])
            require(record in known, 'Native artifact lacks a prior output or compiler receipt')
            result[name] = record
            continue
        require(path.is_absolute() and (path.resolve() in tools or
                any(path.resolve().is_relative_to(base) for base in roots)), 'Executed file outside pinned roots')
        require(not path.resolve().is_relative_to(Path(profile['build_root']).resolve()) or
                any(path.resolve().is_relative_to(base) for base in roots), 'Attempt-specific executable refused')
        require(not any('.build' in path.resolve().relative_to(base).parts
                        for base in roots if path.resolve().is_relative_to(base)), 'Uncommitted build script refused')
        result[name] = record
        pins = {Path(tool).resolve(): sha for tool, sha in profile.get('system_tools', {}).items()}
        if path.resolve() in pins:
            require(result[name]['sha256'] == pins[path.resolve()], 'System executable pin mismatch')
        if path.resolve() in fixture_pins:
            require(record['sha256'] == fixture_pins[path.resolve()], 'Fixture module pin mismatch')
        if 'ap_replay' in profile and path.resolve() == Path(profile['ap_replay']['installed_bundle']).resolve():
            require(plan is not None and record['sha256'] == plan['compat']['ap_replay']['bundle_sha256'],
                    'AP installed bundle pin mismatch')
        if 'native_build' in profile and path.resolve() == Path(profile['native_build']['official_binary']).resolve():
            require(record['sha256'] == profile['native_build']['official_sha256'], 'Official binary pin mismatch')
    return result


def _receipts(root):
    rows = {}
    for path in sorted((Path(root) / 'receipts').glob('*.json')):
        row = load(path)
        keys(row, ('schema_version', 'attempt', 'stage', 'input_key', 'input', 'outputs', 'evidence', 'cleanup'))
        require(row['schema_version'] == 1, 'Unsupported receipt schema')
        require(row.get('stage') in STAGES and path.name == row['stage'] + '.json', 'Invalid receipt name')
        rows[row['stage']] = row
    return rows


def _execution_identity(execution, profile, plan):
    keys(execution, ('job_id', 'executor_package_root', 'executor_sha256', 'architecture',
                    'plus_source_commit', 'tooling_commit', 'outcome', 'exit_code', 'argv', 'cwd', 'env'),
         ('replay_profile_sha256',))
    require(isinstance(execution['job_id'], str) and bool(execution['job_id']), 'Managed job identity required')
    hex_digest(execution['executor_sha256'])
    require(execution['architecture'] == profile['architecture'] and
            execution['executor_package_root'] == profile['executor_package_root'], 'Wrong execution host/executor')
    for name in ('tooling_commit', 'plus_source_commit'):
        require(execution[name] == plan[name], 'Wrong execution source')
    require(execution['outcome'] in ('passed', 'failed', 'unknown') and
            (execution['exit_code'] is None or type(execution['exit_code']) is int), 'Invalid execution outcome')
    require(execution['outcome'] != 'passed' or execution['exit_code'] == 0, 'Success requires known zero exit')
    require(isinstance(execution['argv'], list) and bool(execution['argv']) and
            all(isinstance(arg, str) for arg in execution['argv']) and
            Path(execution['cwd']).is_absolute() and isinstance(execution['env'], dict), 'Invalid command identity')


def _cleanup_identity(root, cleanup, outputs):
    keys(cleanup, ('outcome', 'owned_remaining', 'unknown', 'report'))
    require(cleanup['outcome'] in ('clean', 'blocked', 'failed', 'unknown') and
            all(type(cleanup[name]) is int and cleanup[name] >= 0 for name in ('owned_remaining', 'unknown')),
            'Invalid cleanup evidence')
    require(cleanup['report'] in outputs, 'Cleanup report must be a hash-bound output')
    raw = load(relative_file(root, cleanup['report']))
    normalized = {name: len(raw[name]) if isinstance(raw.get(name), list) else raw.get(name)
                  for name in ('owned_remaining', 'unknown')}
    require(raw.get('outcome') == cleanup['outcome'] and
            all(type(normalized[name]) is int and normalized[name] == cleanup[name] for name in normalized),
            'Cleanup summary differs from raw report')


def write_receipt(root, stage, *, inputs, executed, outputs, evidence, execution, cleanup,
                  replay_profile=None, compiler_artifacts=None, previous_package=None):
    metadata, plan, profile = require_native_attempt(root)
    require(stage in STAGES, 'Unknown release stage')
    keys(execution, ('job_id', 'executor_package_root', 'executor_sha256', 'architecture',
                     'plus_source_commit', 'tooling_commit', 'outcome', 'exit_code', 'argv', 'cwd', 'env'),
         ('replay_profile_sha256',))
    execution_profile = profile
    if replay_profile is not None:
        require(stage == 'ap', 'Only AP permits an external replay profile')
        validate_profile(replay_profile, execution['architecture'])
        require(execution.get('replay_profile_sha256') == digest(replay_profile), 'Replay profile digest mismatch')
        execution_profile = replay_profile
    else:
        require('replay_profile_sha256' not in execution, 'Replay profile is missing')
    _execution_identity(execution, execution_profile, plan)
    keys(cleanup, ('outcome', 'owned_remaining', 'unknown', 'report'))
    require(cleanup['outcome'] in ('clean', 'blocked', 'failed', 'unknown') and
            type(cleanup['owned_remaining']) is int and cleanup['owned_remaining'] >= 0 and
            type(cleanup['unknown']) is int and cleanup['unknown'] >= 0, 'Invalid cleanup evidence')
    output_files = _inventory(root, outputs)
    require(evidence in output_files and cleanup['report'] in output_files, 'Reports must be hash-bound outputs')
    prior = _receipts(root)
    require(stage not in prior, 'Original stage receipt already exists; use a new attempt')
    require(all(STAGES.index(name) < STAGES.index(stage) for name in prior), 'Stage order cannot move backward')
    require(all(name in prior for name in GATES[:GATES.index(stage)]) if stage in GATES else True,
            'Preceding qualification gate is missing')
    input_files = _inventory(root, inputs)
    proof = None
    if compiler_artifacts is not None:
        require(compiler_artifacts in input_files, 'Compiler artifact receipt must be an input')
        proof = load(relative_file(root, compiler_artifacts))
    previous_proof = None
    if previous_package is not None:
        require(previous_package in input_files, 'Previous package proof must be an input')
        previous_proof = load(relative_file(root, previous_package))
    closure = {'attempt': metadata, 'stage': stage, 'inputs': input_files,
               'executed': _executed(execution_profile, executed, root=root, prior=prior,
                                     compiler_artifacts=proof, plan=plan, previous_package=previous_proof), 'execution': execution,
               'previous': {name: digest(row) for name, row in prior.items()}}
    if replay_profile is not None:
        closure['replay_profile'] = replay_profile
    if compiler_artifacts is not None:
        closure['compiler_artifacts'] = compiler_artifacts
    if previous_package is not None:
        closure['previous_package'] = previous_package
    row = {'schema_version': 1, 'attempt': metadata['attempt'], 'stage': stage,
           'input_key': digest(closure), 'input': closure, 'outputs': output_files,
           'evidence': evidence, 'cleanup': cleanup}
    write_once(Path(root) / 'receipts' / (stage + '.json'), row)
    return row


def _transport_manifest(root):
    path = Path(root) / 'evidence-export.json'
    if not path.exists():
        return None
    manifest = load(path)
    keys(manifest, ('schema_version', 'kind', 'included', 'external_artifacts'))
    require(manifest['schema_version'] == 1 and manifest['kind'] == 'evidence-only', 'Invalid evidence export')
    require(not set(manifest['included']) & set(manifest['external_artifacts']), 'Ambiguous exported artifact')
    actual = {path.relative_to(root).as_posix() for path in Path(root).rglob('*') if path.is_file()}
    require(actual == set(manifest['included']) | {'evidence-export.json'}, 'Unexpected/missing exported evidence')
    for name, record in manifest['included'].items():
        expected = dict(record, mode=f'{int(record["mode"], 8) & 0o555:04o}')
        require(file_record(relative_file(root, name)) == expected, 'Exported raw evidence changed')
    required, _, _, _ = _evidence_inputs(root)
    require(required <= manifest['included'].keys(), 'Required raw evidence was made external or omitted')
    require('socket-cleaned.json' in manifest['included'], 'Missing final native cleanup proof')
    _socket_cleanup_proof(root, check_path=False)
    for name, descriptor in manifest['external_artifacts'].items():
        relative_file(root, name)
        keys(descriptor, ('kind', 'record'))
        require(descriptor['kind'] in ('archive', 'package-tree', 'native-output'), 'Invalid external artifact kind')
        keys(descriptor['record'], ('sha256', 'size', 'mode'))
        hex_digest(descriptor['record']['sha256'])
        require(type(descriptor['record']['size']) is int and descriptor['record']['size'] >= 0 and
                isinstance(descriptor['record']['mode'], str) and
                re.fullmatch(r'[0-7]{4}', descriptor['record']['mode']), 'Invalid external artifact record')
    return manifest


def _verify_records(root, records, external, transport=None):
    for name, record in records.items():
        if name in external:
            require(external[name]['record'] == record, 'External artifact differs from native receipt')
        else:
            expected = record
            if transport is not None:
                require(transport['included'].get(name) == record, 'Included evidence differs from native receipt')
                expected = dict(record, mode=f'{int(record["mode"], 8) & 0o555:04o}')
            require(file_record(relative_file(root, name)) == expected, 'Evidence output changed')


def status(root):
    metadata, plan, profile = read_attempt(root)
    rows = _receipts(root)
    transport = _transport_manifest(root)
    external = transport['external_artifacts'] if transport else {}
    _verify_recovery(root, transport)
    previous = {}
    for stage in STAGES:
        if stage not in rows:
            continue
        row = rows[stage]
        closure = row['input']
        keys(closure, ('attempt', 'stage', 'inputs', 'executed', 'execution', 'previous'),
             ('replay_profile', 'compiler_artifacts', 'previous_package'))
        require(row['attempt'] == metadata['attempt'] and closure['attempt'] == metadata and
                closure['stage'] == stage and row['input_key'] == digest(closure) and
                closure['previous'] == previous, 'Receipt identity or input key mismatch')
        _verify_records(root, row['outputs'], external, transport)
        _verify_records(root, closure['inputs'], {}, transport)
        require(row['evidence'] in row['outputs'] and row['cleanup']['report'] in row['outputs'],
                'Unbound receipt report')
        _cleanup_identity(root, row['cleanup'], row['outputs'])
        execution = closure['execution']
        execution_profile = profile
        if 'replay_profile' in closure:
            require(stage == 'ap', 'Replay profile on a native stage')
            execution_profile = validate_profile(closure['replay_profile'], execution['architecture'])
            require(execution.get('replay_profile_sha256') == digest(execution_profile), 'Replay profile binding changed')
        else:
            require('replay_profile_sha256' not in execution, 'Missing replay profile')
        require(execution['architecture'] == execution_profile['architecture'] and
                execution['plus_source_commit'] == plan['plus_source_commit'] and
                execution['tooling_commit'] == plan['tooling_commit'] and
                execution['executor_package_root'] == execution_profile['executor_package_root'], 'Receipt execution identity mismatch')
        previous[stage] = digest(row)
    refused = [stage for stage, row in rows.items() if
               row['input']['execution']['outcome'] != 'passed' or row['input']['execution']['exit_code'] != 0 or
               row['cleanup']['outcome'] != 'clean' or row['cleanup']['owned_remaining'] != 0 or row['cleanup']['unknown'] != 0]
    missing = [stage for stage in GATES if stage not in rows]
    return {'attempt': metadata['attempt'], 'rehearsal': metadata['rehearsal'],
            'receipts': previous, 'missing': missing, 'refused': refused,
            'evidence_only': transport is not None, 'unverified_external': sorted(external),
            'next_action': 'review-failure' if refused else ('record-' + missing[0] if missing else 'accept')}


def _verify_recovery(root, transport):
    path = Path(root) / 'recovery/receipt.json'
    if not path.exists():
        return
    metadata, plan, profile = read_attempt(root)
    receipt = load(path)
    keys(receipt, ('binding', 'files', 'absent', 'audit', 'seals_verified', 'qualification_unchanged'),
         ('result_absent',))
    require(receipt['seals_verified'] is True and receipt['qualification_unchanged'] is True and
            receipt['audit'].get('passed') is True, 'Recovery does not establish unchanged seals and cleanup')
    require({'process-journal.jsonl', 'ownership-baseline.json', 'recovery/managed-state.json',
             'recovery/managed-result.json', 'recovery/managed-resolution.json'} <= receipt['files'].keys(),
            'Recovery snapshots are not hash-bound')
    _verify_records(root, receipt['files'], {}, transport)
    expected = {'attempt': metadata['attempt'],
                'attempt_root': str(Path(profile['build_root']) / 'attempts' / metadata['attempt']),
                'plan_sha256': digest(plan), 'socket_identity': metadata['socket_identity'],
                'journal_sha256': file_record(relative_file(root, 'process-journal.jsonl'))['sha256'],
                'ownership_baseline_sha256': file_record(relative_file(root, 'ownership-baseline.json'))['sha256']}
    require(receipt['binding'] == expected, 'Recovery receipt binding changed')
    state, result, resolution = (load(relative_file(root, 'recovery/managed-' + name + '.json'))
                                 for name in ('state', 'result', 'resolution'))
    require(state.get('result') == result and state.get('resolution') == resolution and
            resolution.get('cleanup_verified') is True and
            type(receipt.get('result_absent', False)) is bool and
            receipt.get('result_absent', False) == (result is None) and
            all(resolution.get('release_recovery', {}).get(name) == value for name, value in expected.items()),
            'Recovery managed snapshots disagree')


def _module(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(Path(path).read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def _check_report(report, required, outputs):
    require(isinstance(report, dict) and isinstance(report.get('checks'), dict), 'Missing dedicated check results')
    require(set(report['checks']) == set(required), 'Missing or unexpected qualification check')
    for value in report['checks'].values():
        keys(value, ('outcome', 'evidence'))
        require(value['outcome'] == 'passed' and isinstance(value['evidence'], dict) and bool(value['evidence']),
                'Qualification check lacks passing raw evidence')
        for name, sha256 in value['evidence'].items():
            require(name in outputs and outputs[name]['sha256'] == sha256, 'Check evidence is not hash-bound')


def _ap_binding(root):
    state = status(root)
    require(state['missing'] == ['ap'] and not state['refused'], 'AP requires passed native gates through captures')
    metadata, plan, _ = read_attempt(root)
    _socket_cleanup_proof(root, check_path=not state['evidence_only'])
    rows = _receipts(root)
    captures = load(relative_file(root, rows['captures']['evidence']))
    return {'attempt': metadata['attempt'], 'plan_sha256': digest(plan), 'architecture': metadata['architecture'],
            'rehearsal': metadata['rehearsal'], 'receipts': state['receipts'],
            'package_sha256': captures['package_sha256'],
            'capture_manifest_sha256': captures['capture_manifest_sha256'],
            'socket_cleanup_sha256': file_record(Path(root) / 'socket-cleaned.json')['sha256'],
            'ap_replay': plan['compat']['ap_replay']}


def write_ap_receipt(native_root, coordinator_root, *, inputs, executed, outputs, evidence,
                     execution, cleanup, replay_profile):
    """Record coordinator AP evidence separately; never modify the native import."""
    coordinator_root = Path(coordinator_root)
    require(coordinator_root.is_dir() and not coordinator_root.resolve().is_relative_to(Path(native_root).resolve()),
            'Coordinator AP evidence must be outside the native attempt')
    binding = _ap_binding(native_root)
    _, plan, _ = read_attempt(native_root)
    validate_profile(replay_profile, execution['architecture'])
    require('ap_replay' in replay_profile, 'AP replay host paths are required')
    _execution_identity(execution, replay_profile, plan)
    require(execution.get('replay_profile_sha256') == digest(replay_profile), 'Replay profile digest mismatch')
    output_files = _inventory(coordinator_root, outputs)
    require(evidence in output_files, 'AP report must be a hash-bound output')
    _cleanup_identity(coordinator_root, cleanup, output_files)
    closure = {'native': binding, 'replay_profile': replay_profile, 'execution': execution,
               'inputs': _inventory(coordinator_root, inputs),
               'executed': _executed(replay_profile, executed, root=coordinator_root, plan=plan)}
    row = {'schema_version': 1, 'attempt': binding['attempt'], 'stage': 'ap',
           'input_key': digest(closure), 'input': closure, 'outputs': output_files,
           'evidence': evidence, 'cleanup': cleanup}
    write_once(coordinator_root / 'ap-receipt.json', row)
    return row


def _read_ap_receipt(native_root, path):
    path = Path(path)
    row = load(path)
    keys(row, ('schema_version', 'attempt', 'stage', 'input_key', 'input', 'outputs', 'evidence', 'cleanup'))
    closure = row['input']
    keys(closure, ('native', 'replay_profile', 'execution', 'inputs', 'executed'))
    require(row['schema_version'] == 1 and row['stage'] == 'ap' and row['input_key'] == digest(closure) and
            closure['native'] == _ap_binding(native_root) and row['attempt'] == closure['native']['attempt'],
            'External AP receipt native binding changed')
    _, plan, _ = read_attempt(native_root)
    profile, execution = closure['replay_profile'], closure['execution']
    validate_profile(profile, execution['architecture'])
    _execution_identity(execution, profile, plan)
    require('ap_replay' in profile and execution.get('replay_profile_sha256') == digest(profile),
            'Replay profile digest mismatch')
    require(bool(closure['executed']), 'AP executed closure is missing')
    _verify_records(path.parent, closure['inputs'], {})
    _verify_records(path.parent, row['outputs'], {})
    require(row['evidence'] in row['outputs'], 'AP report is not hash-bound')
    _cleanup_identity(path.parent, row['cleanup'], row['outputs'])
    return row


def _accept_archive(root, row, report, plan, arch, verifier, archive):
    name = report['archive']
    require(name in row['outputs'], 'Archive not hash-bound')
    expected = row['outputs'][name]
    actual = file_record(archive)
    require(all(actual[key] == expected[key] for key in ('sha256', 'size')), 'External archive hash/size mismatch')
    package_name = Path(report['package_dir']).name
    with tempfile.TemporaryDirectory(prefix='cp-accepted-package-') as temporary:
        scratch = Path(temporary)
        seen = set()
        with tarfile.open(archive, 'r:*') as tar:
            for member in tar:
                require(member.isdir() or member.isfile(), 'Unsupported archive entry')
                path = relative_file(scratch, member.name)
                require(Path(member.name).parts[0] == package_name, 'Unexpected archive root')
                canonical = Path(member.name).as_posix()
                require(canonical not in seen, 'Duplicate archive member')
                seen.add(canonical)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    require(member.mode & ~0o777 == 0, 'Unsafe package file mode')
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with path.open('xb') as output, tar.extractfile(member) as source:
                        shutil.copyfileobj(source, output)
                    path.chmod(member.mode)
        directory = scratch / package_name
        prefix = report['package_dir'].rstrip('/') + '/'
        original = {name[len(prefix):]: record for name, record in row['outputs'].items() if name.startswith(prefix)}
        actual_tree = {path.relative_to(directory).as_posix(): file_record(path)
                       for path in directory.rglob('*') if path.is_file()}
        require(actual_tree == original, 'Archive differs from verified package inventory')
        manifest = verifier.verify_package(directory)
        require(manifest['plus_commit'] == plan['plus_source_commit'] and manifest['release'] == plan['release'] and
                manifest['target'] == plan['targets'][arch]['triple'] and manifest['upstream'] == plan['upstream'] and
                manifest['frontend_patch_sha256'] == plan['patch']['sha256'] and
                manifest['build']['glibc'] == plan['targets'][arch]['build_glibc'], 'Package identity mismatch')
        return manifest, file_record(directory / 'codex-plus.json')['sha256']


def _accept_gates(root, *, source_root, archive=None, gates=GATES, ap_receipt=None):
    state = status(root)
    metadata, plan, profile = read_attempt(root)
    rows = _receipts(root)
    receipts = dict(state['receipts'])
    ap_root = None
    if ap_receipt is not None:
        require('ap' not in rows and 'ap' in gates, 'Ambiguous or unexpected external AP receipt')
        rows['ap'] = _read_ap_receipt(root, ap_receipt)
        receipts['ap'] = digest(rows['ap'])
        ap_root = Path(ap_receipt).parent
    refused = [stage for stage in (*STAGES[:3], *gates) if stage in rows and (
        rows[stage]['input']['execution']['outcome'] != 'passed' or
        rows[stage]['input']['execution']['exit_code'] != 0 or
        rows[stage]['cleanup']['outcome'] != 'clean' or
        rows[stage]['cleanup']['owned_remaining'] != 0 or rows[stage]['cleanup']['unknown'] != 0)]
    require(all(gate in rows for gate in gates) and not refused, 'Missing/failed/unknown gate or cleanup')
    _socket_cleanup_proof(root, check_path=not state['evidence_only'])
    validate_plan(plan, source_root)
    require(not metadata['rehearsal'], 'Rehearsal cannot qualify final evidence')
    arch = metadata['architecture']
    # The product-source verifier knows that release's package constants.
    package = _module(Path(source_root) / 'scripts/package-release.py')
    native = _module(Path(source_root) / 'scripts/native-results.py')
    manifest = None
    archive_path = Path(archive) if archive is not None else None
    archive_name = None
    manifest_sha256 = None
    for gate in gates:
        row = rows[gate]
        report_root = ap_root if gate == 'ap' and ap_root is not None else root
        report = load(relative_file(report_root, row['evidence']))
        require(report.get('architecture') == arch and report.get('plus_source_commit') == plan['plus_source_commit'],
                'Gate report source/architecture mismatch')
        spec = plan['gates'][arch][gate]
        if gate == 'native-tests':
            require(set(report.get('suites', {})) == set(spec['suites']), 'Missing native suite')
            for name, expected in spec['suites'].items():
                suite = report['suites'][name]
                for field in ('listed', 'stdout', 'logfile'):
                    require(suite[field] in row['outputs'], 'Native records not bound to receipt')
                result = native.validate_test_output(name,
                    relative_file(root, suite['listed']).read_text(),
                    relative_file(root, suite['stdout']).read_text(),
                    relative_file(root, suite['logfile']).read_text(), exit_code=suite['exit_code'],
                    allowed_ignored=expected['allowed_ignored'])
                require(result['passed'] == expected['passed'], 'Native result requirements differ')
        else:
            _check_report(report, spec['checks'], row['outputs'])
        if gate == 'package':
            archive_name = report['archive']
            require(not state['evidence_only'] or archive_path is not None, 'Imported evidence requires separate actual archive')
            selected = archive_path if archive_path is not None else relative_file(root, archive_name)
            manifest, manifest_sha256 = _accept_archive(root, row, report, plan, arch, package, selected)
        if gate == 'ap':
            require(report.get('ap_version') == plan['compat']['ap_version'], 'AP replay version mismatch')
            if 'ap_replay' in plan['compat']:
                require(report.get('ap_replay') == plan['compat']['ap_replay'], 'AP source/plugin pins mismatch')
            capture_row = rows['captures']
            captures = load(relative_file(root, capture_row['evidence']))
            require(report.get('capture_manifest_sha256') == captures.get('capture_manifest_sha256') and
                    report.get('package_sha256') == rows['package']['outputs'][archive_name]['sha256'],
                    'AP replay capture/package binding mismatch')
        if gate == 'captures':
            require(report.get('package_sha256') == rows['package']['outputs'][archive_name]['sha256'],
                    'Captures are not from the accepted package')
            name = report.get('capture_manifest')
            require(name in row['outputs'] and row['outputs'][name]['sha256'] == report.get('capture_manifest_sha256'),
                    'Capture manifest not hash-bound')
            captures = load(relative_file(root, name))
            require(captures.get('architecture') == arch and captures.get('package_sha256') == report['package_sha256'] and
                    isinstance(captures.get('files'), dict) and bool(captures['files']), 'Invalid capture manifest')
            for name, record in captures['files'].items():
                require(name in row['outputs'] and row['outputs'][name] == record, 'Capture file not hash-bound')
    return {'accepted': True, 'attempt': metadata['attempt'], 'plan_sha256': digest(plan),
            'architecture': arch, 'rehearsal': False, 'receipts': receipts,
            'coordinator_protocol': manifest['coordinator_protocol'],
            'execution_record_format': manifest['execution_record_format'],
            'target': {'triple': manifest['target'], 'asset': Path(archive_name).name,
                       'sha256': rows['package']['outputs'][archive_name]['sha256'],
                       'manifest_sha256': manifest_sha256,
                       'tree_sha256': manifest['tree_sha256']}}


def accept_attempt(root, *, source_root, archive=None, ap_receipt=None):
    return _accept_gates(root, source_root=source_root, archive=archive, ap_receipt=ap_receipt)


def approve_package(root, *, source_root, archive=None):
    accepted = _accept_gates(root, source_root=source_root, archive=archive, gates=GATES[:2])
    _, plan, _ = read_attempt(root)
    return {'accepted': True, 'cleanup_passed': True, 'native': True,
            'source': plan['plus_source_commit'], 'architecture': accepted['architecture'],
            'archive_sha256': accepted['target']['sha256'],
            'manifest_sha256': accepted['target']['manifest_sha256'],
            'approval_scope': 'package-native-tests', 'attempt': accepted['attempt'],
            'plan_sha256': accepted['plan_sha256'], 'receipts': accepted['receipts']}


def make_handoff(plan, accepted_by_arch):
    require(set(accepted_by_arch) == set(TARGETS), 'Both accepted native targets required')
    for arch, accepted in accepted_by_arch.items():
        require(accepted.get('accepted') is True and accepted.get('rehearsal') is False and
                accepted.get('architecture') == arch and accepted.get('plan_sha256') == digest(plan) and
                set(GATES) <= accepted.get('receipts', {}).keys(), 'Unaccepted or rehearsal handoff input')
        keys(accepted['target'], ('triple', 'asset', 'sha256', 'manifest_sha256', 'tree_sha256'))
        require(accepted['target']['triple'] == plan['targets'][arch]['triple'] and
                Path(accepted['target']['asset']).name == accepted['target']['asset'], 'Wrong handoff target')
        for name in ('sha256', 'manifest_sha256', 'tree_sha256'):
            hex_digest(accepted['target'][name])
    first = accepted_by_arch['x86_64']
    require(all(row['coordinator_protocol'] == first['coordinator_protocol'] and
                row['execution_record_format'] == first['execution_record_format']
                for row in accepted_by_arch.values()), 'Package protocols disagree')
    return {'release': plan['release'], 'plus_commit': plan['plus_source_commit'],
            'coordinator_protocol': first['coordinator_protocol'],
            'execution_record_format': first['execution_record_format'], 'upstream': plan['upstream'],
            'note': plan['note'], 'targets': {arch: row['target'] for arch, row in accepted_by_arch.items()},
            'rollout': plan['rollout']}


def _evidence_inputs(root):
    required = {'attempt.json', 'plan.json', 'profile.json'}
    for name in ('process-journal.jsonl', 'ownership-baseline.json', 'native-preparation.json'):
        if (Path(root) / name).exists():
            required.add(name)
    required.update(path.relative_to(root).as_posix() for path in (Path(root) / 'native-starts').glob('*.json'))
    outputs = {}
    archives, packages = set(), set()
    for stage, row in _receipts(root).items():
        required.update((f'receipts/{stage}.json', row['evidence'], row['cleanup']['report']))
        required.update(row['input']['inputs'])
        outputs.update(row['outputs'])
        report = load(relative_file(root, row['evidence']))
        if stage in GATES:
            for check in report.get('checks', {}).values():
                required.update(check.get('evidence', {}))
        if stage == 'native-tests':
            for suite in report.get('suites', {}).values():
                required.update(suite[field] for field in ('listed', 'stdout', 'logfile'))
        if 'capture_manifest' in report:
            required.add(report['capture_manifest'])
            required.update(load(relative_file(root, report['capture_manifest']))['files'])
        if stage == 'package' and 'archive' in report:
            archives.add(report['archive'])
            if 'package_dir' in report:
                packages.add(report['package_dir'].rstrip('/') + '/')
    for directory in ('socket-creation-intents', 'socket-creation-results', 'socket-cleanup-retries'):
        required.update(path.relative_to(root).as_posix() for path in (Path(root) / directory).glob('*.json'))
    for name in ('socket-removal.json', 'socket-removal-intent.json', 'socket-quarantine.json',
                 'socket-moved.json', 'socket-deleted.json'):
        if (Path(root) / name).exists():
            required.add(name)
    if (Path(root) / 'socket-cleaned.json').exists():
        required.add('socket-cleaned.json')
        required.add(load(Path(root) / 'socket-cleaned.json')['report'])
    if (Path(root) / 'recovery/receipt.json').exists():
        required.add('recovery/receipt.json')
        required.update(load(Path(root) / 'recovery/receipt.json')['files'])
    return required, outputs, archives, packages


def _evidence_closure(root):
    """Transport raw evidence, while retaining native artifact records as metadata."""
    required, outputs, archives, packages = _evidence_inputs(root)
    external = {}
    included = set(required)
    for name in required:
        require(not any(part in ('source', 'upstream', 'cache', 'cargo-target', 'license-cache')
                        for part in Path(name).parts), f'Raw evidence cannot require a source/cache tree: {name}')
    for name, record in outputs.items():
        parts = Path(name).parts
        kind = ('archive' if name in archives else
                'package-tree' if any(name.startswith(prefix) for prefix in packages) else
                'native-output' if any(part in ('source', 'upstream', 'native', 'extracted', 'packages',
                    'test-binaries', 'cache', 'cargo-target', 'license-cache') for part in parts) else None)
        with relative_file(root, name).open('rb') as stream:
            elf = stream.read(4) == b'\x7fELF'
        if elf:
            kind = 'native-output'
        if name in required and name not in archives and not elf:
            kind = None
        if kind:
            require(name not in required, f'Required raw evidence cannot be external: {name}')
            external[name] = {'kind': kind, 'record': record}
        else:
            included.add(name)
    return {'schema_version': 1, 'kind': 'evidence-only',
            'included': _inventory(root, sorted(included)), 'external_artifacts': external}


def export_attempt(root, destination):
    require_native_attempt(root)
    status(root)
    _socket_cleanup_proof(root)
    root, destination = Path(root), Path(destination)
    require(not destination.exists(), 'Export already exists')
    require(not destination.resolve().is_relative_to(root.resolve()), 'Export must be outside the attempt')
    manifest = _evidence_closure(root)
    with destination.open('xb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz') as tar:
            for name in sorted(manifest['included']):
                tar.add(relative_file(root, name), arcname=name, recursive=False)
            payload = encoded(manifest)
            member = tarfile.TarInfo('evidence-export.json')
            member.size, member.mode = len(payload), 0o400
            tar.addfile(member, io.BytesIO(payload))
    return file_record(destination)


def import_attempt(bundle, destination, *, expected_sha256):
    hex_digest(expected_sha256)
    require(file_record(bundle)['sha256'] == expected_sha256, 'Transport hash mismatch')
    destination = Path(destination)
    require(not destination.exists(), 'Import destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        scratch = Path(temporary) / 'attempt'
        scratch.mkdir(mode=0o700)
        with tarfile.open(bundle, 'r:*') as tar:
            names = set()
            for member in tar:
                relative_file(scratch, member.name)
                require(member.name not in names and (member.isfile() or member.isdir()), 'Unsafe/duplicate evidence member')
                names.add(member.name)
                path = scratch / member.name
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with path.open('xb') as output, tar.extractfile(member) as source:
                        shutil.copyfileobj(source, output)
                    path.chmod(member.mode & 0o555)
        require((scratch / 'evidence-export.json').is_file(), 'Missing evidence-only export manifest')
        status(scratch)
        for path in sorted(scratch.rglob('*'), key=lambda path: len(path.parts), reverse=True):
            if path.is_dir():
                path.chmod(0o500)
        scratch.rename(destination)
        destination.chmod(0o500)
    return destination


def _socket_cleanup_proof(root, *, check_path=True):
    metadata, _, _ = read_attempt(root)
    identity = metadata['socket_identity']
    if check_path:
        require(not os.path.lexists(identity['path']), 'Socket directory remains; final native cleanup is required')
    proof = load(Path(root) / 'socket-cleaned.json')
    keys(proof, ('outcome', 'socket_identity', 'report', 'report_sha256'))
    require(proof['outcome'] == 'clean' and proof['socket_identity'] == identity, 'Unresolved socket cleanup proof')
    report_path = relative_file(root, proof['report'])
    require(file_record(report_path)['sha256'] == proof['report_sha256'], 'Final cleanup report changed')
    report = load(report_path)
    require(report.get('outcome') == 'clean' and report.get('socket_identity') == identity and
            report.get('socket_removed') is True and all((type(report.get(name)) is int and report[name] == 0) or report.get(name) == []
            for name in ('owned_remaining', 'unknown')), 'Final native cleanup did not resolve ownership')
    recovery = Path(root) / 'recovery/receipt.json'
    if recovery.exists() or 'recovery_receipt' in report:
        require(report.get('recovery_receipt') == 'recovery/receipt.json' and
                report.get('recovery_receipt_sha256') == file_record(recovery)['sha256'],
                'Final cleanup recovery receipt changed')
    return proof


def seal_socket_cleanup(root, report):
    metadata, _, _ = require_native_attempt(root)
    proof = {'outcome': 'clean', 'socket_identity': metadata['socket_identity'], 'report': report,
             'report_sha256': file_record(relative_file(root, report))['sha256']}
    # The owning native cleanup hook removes the directory; this records its proof.
    require(not os.path.lexists(metadata['socket_root']), 'Socket directory remains')
    raw = load(relative_file(root, report))
    require(raw.get('outcome') == 'clean' and raw.get('socket_identity') == metadata['socket_identity'] and
            raw.get('socket_removed') is True and all((type(raw.get(name)) is int and raw[name] == 0) or raw.get(name) == []
            for name in ('owned_remaining', 'unknown')), 'Final native cleanup did not resolve ownership')
    write_once(Path(root) / 'socket-cleaned.json', proof)
    return proof


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('plan-check', 'status', 'export', 'import', 'approve-package', 'accept', 'handoff'):
        command = commands.add_parser(name)
        if name in ('plan-check', 'handoff'):
            command.add_argument('--plan', type=Path, required=True)
        if name in ('plan-check', 'approve-package', 'accept', 'handoff'):
            command.add_argument('--source-root', type=Path, required=True)
        if name in ('status', 'export', 'approve-package', 'accept'):
            command.add_argument('--attempt', type=Path, required=True)
        if name in ('export', 'import', 'approve-package', 'accept', 'handoff'):
            command.add_argument('--output', type=Path, required=True)
        if name == 'import':
            command.add_argument('--bundle', type=Path, required=True)
            command.add_argument('--sha256', required=True, help='Separately received evidence-bundle digest')
        if name in ('accept', 'approve-package'):
            command.add_argument('--archive', type=Path)
        if name == 'accept':
            command.add_argument('--ap-receipt', type=Path)
        if name == 'handoff':
            command.add_argument('--accept', action='append', required=True, metavar='ARCH=PATH')
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan-check':
            plan = validate_plan(load(args.plan), args.source_root)
            result = {'valid': True, 'plan_sha256': digest(plan)}
        elif args.command == 'status':
            result = status(args.attempt)
        elif args.command == 'export':
            result = export_attempt(args.attempt, args.output)
        elif args.command == 'import':
            imported = import_attempt(args.bundle, args.output, expected_sha256=args.sha256)
            result = status(imported)
        elif args.command in ('approve-package', 'accept'):
            result = (approve_package(args.attempt, source_root=args.source_root, archive=args.archive)
                      if args.command == 'approve-package' else
                      accept_attempt(args.attempt, source_root=args.source_root, archive=args.archive,
                                     ap_receipt=args.ap_receipt))
            write_once(args.output, result)
        else:
            plan = validate_plan(load(args.plan), args.source_root)
            accepted = {}
            for binding in args.accept:
                arch, separator, path = binding.partition('=')
                require(separator and arch not in accepted, 'Expected unique ARCH=PATH acceptance bindings')
                accepted[arch] = load(path)
            result = make_handoff(plan, accepted)
            write_once(args.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'accepted': False, 'error': str(error)}, sort_keys=True))
        return 1


if __name__ == '__main__':
    sys.exit(main())
