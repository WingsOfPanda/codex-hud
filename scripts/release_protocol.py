"""Immutable, local release evidence. This module never starts a process.

Receipts establish consistency of supplied native evidence, not host attestation.
Transport must supply the export digest through the owning session separately.
"""
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import string
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
                          'previous_source_commit', 'stock_binary_sha256', 'fixture_files'))
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
         ('native_build', 'system_tools'))
    require(profile['schema_version'] == 1 and profile['architecture'] == arch and
            profile['triple'] == TARGETS.get(arch), 'Wrong native profile architecture')
    require(all(isinstance(profile[name], str) and profile[name] for name in ('host_id', 'hostname')),
            'Missing native host identity')
    for name in ('build_root', 'short_root', 'tooling_root', 'source_root', 'executor_package_root'):
        require(isinstance(profile[name], str) and Path(profile[name]).is_absolute() and
                '..' not in Path(profile[name]).parts, f'Invalid host path: {name}')
    keys(profile['toolchain'], ('rustc', 'cargo', 'version'))
    keys(profile['python'], ('path', 'version', 'libpython'))
    for path in (profile['toolchain']['rustc'], profile['toolchain']['cargo'], profile['python']['path']):
        require(isinstance(path, str) and Path(path).is_absolute(), 'Tool must have an absolute pin')
    require(profile['python']['libpython'] is None or Path(profile['python']['libpython']).is_absolute(),
            'Invalid libpython pin')
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
    short_root = Path(profile['short_root'])
    short_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_root = None
    for _ in range(64):
        candidate = short_root / ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(2))
        try:
            candidate.mkdir(mode=0o700)
            socket_root = candidate
            break
        except FileExistsError:
            continue
    require(socket_root is not None, 'Private socket namespace is full')
    metadata = {'schema_version': 1, 'attempt': attempt_id, 'architecture': arch,
                'plan_sha256': digest(plan), 'profile_sha256': digest(profile),
                'created_at': datetime.now(timezone.utc).isoformat(), 'rehearsal': rehearsal,
                'native': observed, 'socket_root': str(socket_root)}
    write_once(root / 'plan.json', plan)
    write_once(root / 'profile.json', profile)
    write_once(root / 'attempt.json', metadata)
    return root


def read_attempt(root):
    root = Path(root)
    metadata, plan, profile = (load(root / name) for name in ('attempt.json', 'plan.json', 'profile.json'))
    keys(metadata, ('schema_version', 'attempt', 'architecture', 'plan_sha256', 'profile_sha256',
                    'created_at', 'rehearsal', 'native', 'socket_root'))
    require(metadata['schema_version'] == 1 and type(metadata['rehearsal']) is bool, 'Invalid attempt metadata')
    require(metadata['plan_sha256'] == digest(plan) and metadata['profile_sha256'] == digest(profile),
            'Attempt input tampering')
    require(metadata['architecture'] == profile['architecture'], 'Attempt architecture mismatch')
    validate_profile(profile, metadata['architecture'], metadata['native'])
    require(digest(profile) == plan['targets'][metadata['architecture']]['profile_sha256'], 'Profile binding mismatch')
    for name in ('tooling_commit', 'plus_source_commit'):
        require(metadata['native'][name] == plan[name], 'Attempt source identity mismatch')
    return metadata, plan, profile


def _inventory(root, names):
    require(isinstance(names, list) and bool(names) and len(names) == len(set(names)), 'Evidence files required')
    return {name: file_record(relative_file(root, name)) for name in sorted(names)}


def _executed(profile, paths, *, root=None, prior=None, compiler_artifacts=None, plan=None):
    roots = [Path(profile[name]).resolve() for name in ('tooling_root', 'source_root', 'executor_package_root')]
    tools = {Path(profile['toolchain'][name]).resolve() for name in ('rustc', 'cargo')}
    tools.add(Path(profile['python']['path']).resolve())
    tools.update(Path(name).resolve() for name in profile.get('system_tools', {}))
    if 'native_build' in profile:
        tools.add(Path(profile['native_build']['official_binary']).resolve())
    require(bool(paths), 'Executed file closure required')
    result = {}
    for name in paths:
        path = Path(name)
        record = file_record(path)
        if root is not None and path.resolve().is_relative_to(Path(root).resolve()):
            relative = path.resolve().relative_to(Path(root).resolve()).as_posix()
            relative_file(root, relative)
            if path.name == 'codex-plus' and path.parent.name == 'bin':
                manifest = load(path.parent.parent / 'codex-plus.json')
                require(record == file_record(Path(profile['source_root']) / 'bin/codex-plus') and
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


def write_receipt(root, stage, *, inputs, executed, outputs, evidence, execution, cleanup,
                  replay_profile=None, compiler_artifacts=None):
    metadata, plan, profile = read_attempt(root)
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
    require(isinstance(execution['job_id'], str) and bool(execution['job_id']), 'Managed job identity required')
    hex_digest(execution['executor_sha256'])
    require(execution['architecture'] == execution_profile['architecture'] and
            execution['executor_package_root'] == execution_profile['executor_package_root'], 'Wrong execution host/executor')
    for name in ('tooling_commit', 'plus_source_commit'):
        require(execution[name] == plan[name], 'Wrong execution source')
    require(execution['outcome'] in ('passed', 'failed', 'unknown') and
            (execution['exit_code'] is None or type(execution['exit_code']) is int), 'Invalid execution outcome')
    require(execution['outcome'] != 'passed' or execution['exit_code'] == 0, 'Success requires known zero exit')
    require(isinstance(execution['argv'], list) and bool(execution['argv']) and
            all(isinstance(arg, str) for arg in execution['argv']) and
            Path(execution['cwd']).is_absolute() and isinstance(execution['env'], dict), 'Invalid command identity')
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
    closure = {'attempt': metadata, 'stage': stage, 'inputs': input_files,
               'executed': _executed(execution_profile, executed, root=root, prior=prior,
                                     compiler_artifacts=proof, plan=plan), 'execution': execution,
               'previous': {name: digest(row) for name, row in prior.items()}}
    if replay_profile is not None:
        closure['replay_profile'] = replay_profile
    if compiler_artifacts is not None:
        closure['compiler_artifacts'] = compiler_artifacts
    row = {'schema_version': 1, 'attempt': metadata['attempt'], 'stage': stage,
           'input_key': digest(closure), 'input': closure, 'outputs': output_files,
           'evidence': evidence, 'cleanup': cleanup}
    write_once(Path(root) / 'receipts' / (stage + '.json'), row)
    return row


def status(root):
    metadata, plan, profile = read_attempt(root)
    rows = _receipts(root)
    previous = {}
    for stage in STAGES:
        if stage not in rows:
            continue
        row = rows[stage]
        closure = row['input']
        keys(closure, ('attempt', 'stage', 'inputs', 'executed', 'execution', 'previous'),
             ('replay_profile', 'compiler_artifacts'))
        require(row['attempt'] == metadata['attempt'] and closure['attempt'] == metadata and
                closure['stage'] == stage and row['input_key'] == digest(closure) and
                closure['previous'] == previous, 'Receipt identity or input key mismatch')
        for collection in ('outputs',):
            require(row[collection] == _inventory(root, list(row[collection])), 'Evidence output changed')
        require(closure['inputs'] == _inventory(root, list(closure['inputs'])), 'Evidence input changed')
        require(row['evidence'] in row['outputs'] and row['cleanup']['report'] in row['outputs'],
                'Unbound receipt report')
        cleanup_report = load(relative_file(root, row['cleanup']['report']))
        normalized = {name: len(cleanup_report[name]) if isinstance(cleanup_report.get(name), list)
                      else cleanup_report.get(name) for name in ('owned_remaining', 'unknown')}
        require(cleanup_report.get('outcome') == row['cleanup']['outcome'] and
                all(normalized[name] == row['cleanup'][name] for name in normalized),
                'Cleanup summary differs from raw report')
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
            'next_action': 'review-failure' if refused else ('record-' + missing[0] if missing else 'accept')}


def _module(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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


def accept_attempt(root, *, source_root):
    state = status(root)
    require(not state['missing'] and not state['refused'], 'Missing/failed/unknown gate or cleanup')
    metadata, plan, profile = read_attempt(root)
    validate_plan(plan, source_root)
    require(not metadata['rehearsal'], 'Rehearsal cannot qualify final evidence')
    arch = metadata['architecture']
    rows = _receipts(root)
    # The product-source verifier knows that release's package constants.
    package = _module(Path(source_root) / 'scripts/package-release.py')
    native = _module(Path(source_root) / 'scripts/native-results.py')
    manifest = None
    archive = None
    for gate in GATES:
        row = rows[gate]
        report = load(relative_file(root, row['evidence']))
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
                require(result['passed'] == expected['passed'] and result['filtered'] == 0,
                        'Native result requirements differ')
        else:
            _check_report(report, spec['checks'], row['outputs'])
        if gate == 'package':
            archive = report['archive']
            require(archive in row['outputs'], 'Archive not hash-bound')
            directory = relative_file(root, report['package_dir'])
            manifest = package.verify_package(directory)
            for path in directory.rglob('*'):
                if path.is_file():
                    require(path.relative_to(Path(root)).as_posix() in row['outputs'], 'Package file not hash-bound')
            require(manifest['plus_commit'] == plan['plus_source_commit'] and manifest['release'] == plan['release'] and
                    manifest['target'] == plan['targets'][arch]['triple'] and manifest['upstream'] == plan['upstream'] and
                    manifest['frontend_patch_sha256'] == plan['patch']['sha256'] and
                    manifest['build']['glibc'] == plan['targets'][arch]['build_glibc'], 'Package identity mismatch')
            # Archive bytes must contain exactly the verified extracted package, including modes.
            with tarfile.open(relative_file(root, archive), 'r:*') as tar:
                members = {}
                for member in tar:
                    require(member.isdir() or member.isfile(), 'Unsupported archive entry')
                    if not member.isfile():
                        continue
                    parts = Path(member.name).parts
                    require(parts and '..' not in parts and not Path(member.name).is_absolute(), 'Unsafe archive member')
                    require(parts[0] == directory.name, 'Unexpected archive root')
                    relative = Path(*parts[1:]).as_posix()
                    require(relative not in members, 'Duplicate archive member')
                    stream = tar.extractfile(member)
                    h = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(chunk)
                    members[relative] = {'sha256': h.hexdigest(), 'size': member.size, 'mode': f'{member.mode:04o}'}
                expected_files = {path.relative_to(directory).as_posix(): file_record(path)
                                  for path in directory.rglob('*') if path.is_file()}
                require(members == expected_files, 'Archive differs from verified package')
        if gate == 'ap':
            require(report.get('ap_version') == plan['compat']['ap_version'], 'AP replay version mismatch')
            capture_row = rows['captures']
            captures = load(relative_file(root, capture_row['evidence']))
            require(report.get('capture_manifest_sha256') == captures.get('capture_manifest_sha256') and
                    report.get('package_sha256') == rows['package']['outputs'][archive]['sha256'],
                    'AP replay capture/package binding mismatch')
        if gate == 'captures':
            require(report.get('package_sha256') == rows['package']['outputs'][archive]['sha256'],
                    'Captures are not from the accepted package')
            name = report.get('capture_manifest')
            require(name in row['outputs'] and row['outputs'][name]['sha256'] == report.get('capture_manifest_sha256'),
                    'Capture manifest not hash-bound')
            captures = load(relative_file(root, name))
            require(captures.get('architecture') == arch and captures.get('package_sha256') == report['package_sha256'] and
                    isinstance(captures.get('files'), dict) and bool(captures['files']), 'Invalid capture manifest')
            for name, record in captures['files'].items():
                require(name in row['outputs'] and row['outputs'][name] == record, 'Capture file not hash-bound')
    manifest_path = relative_file(root, load(relative_file(root, rows['package']['evidence']))['package_dir']) / 'codex-plus.json'
    return {'accepted': True, 'attempt': metadata['attempt'], 'plan_sha256': digest(plan),
            'architecture': arch, 'rehearsal': False, 'receipts': state['receipts'],
            'coordinator_protocol': manifest['coordinator_protocol'],
            'execution_record_format': manifest['execution_record_format'],
            'target': {'triple': manifest['target'], 'asset': Path(archive).name,
                       'sha256': rows['package']['outputs'][archive]['sha256'],
                       'manifest_sha256': file_record(manifest_path)['sha256'],
                       'tree_sha256': manifest['tree_sha256']}}


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


def export_attempt(root, destination):
    status(root)
    root, destination = Path(root), Path(destination)
    require(not destination.exists(), 'Export already exists')
    require(not destination.resolve().is_relative_to(root.resolve()), 'Export must be outside the attempt')
    with destination.open('xb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz') as tar:
            for path in sorted(root.rglob('*')):
                require(not path.is_symlink() and (path.is_file() or path.is_dir()), 'Unsupported evidence entry')
                tar.add(path, arcname=path.relative_to(root), recursive=False)
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
                    path.chmod(member.mode & 0o777)
        status(scratch)
        scratch.rename(destination)
    return destination
