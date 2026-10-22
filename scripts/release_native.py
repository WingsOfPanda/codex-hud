"""Local native release preparation and fixed recipes; launch belongs to job launch.

The qualified recipe is for upstream 0.163.0. New upstream families need review.
Failed or interrupted stages are retained and never replayed in the same attempt.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import release_protocol as protocol


STAGES = ('preflight', 'frontend', 'runtime', 'package', 'native-tests')
RECIPE = Path(__file__).resolve().parent / 'release_recipes/native'
NATIVE_KEYS = ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache',
               'license_cache', 'cargo_jobs', 'test_threads', 'official_binary', 'official_sha256')
SYSTEM_TOOLS = ('/usr/bin/git', '/usr/bin/strip', '/usr/bin/readelf', '/usr/bin/ldd',
                '/usr/bin/getconf', '/usr/bin/tar', '/bin/sh', '/usr/bin/pkg-config')


def module(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem.replace('-', '_'), path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def sha(path):
    return protocol.file_record(path)['sha256']


def python_env(profile):
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    source = Path(profile['source_root'])
    env['PYTHONPATH'] = os.pathsep.join([str(source), str(source / 'tests'),
                                       *profile['python'].get('fixture_sites', [])])
    library = profile['python']['libpython']
    if library is not None:
        env['LD_LIBRARY_PATH'] = str(Path(library).parent)
    return env


def fixture_closure(profile):
    pins = profile['python'].get('fixture_imports', {})
    protocol.require(set(pins) == {'pyte', 'wcwidth'}, 'Pin pyte and wcwidth fixture imports')
    sites = [Path(path).resolve() for path in profile['python'].get('fixture_sites', [])]
    protocol.require(sites and all(path.is_dir() for path in sites), 'Missing fixture site directory')
    result = {}
    for name, pin in pins.items():
        origin = Path(pin['origin']).resolve()
        protocol.require(any(origin.is_relative_to(site) for site in sites), 'Fixture origin is outside fixture sites')
        protocol.require(sha(origin) == pin['sha256'], f'Fixture import hash mismatch: {name}')
        paths = origin.parent.rglob('*.py') if origin.name == '__init__.py' else [origin]
        for path in paths:
            result[str(path.resolve())] = protocol.file_record(path)
    return result


def fixture_import_probe(profile):
    """Check imports using the exact child interpreter and the existing validator."""
    fixture_closure(profile)
    imports = [{'module': name, 'origin': pin['origin'],
                'symbols': ['Screen', 'Stream'] if name == 'pyte' else ['wcwidth']}
               for name, pin in profile['python']['fixture_imports'].items()]
    validator = str(Path(profile['source_root']) / 'scripts/release-preflight.py')
    code = ('import importlib.util,json,sys;spec=importlib.util.spec_from_file_location("preflight",sys.argv[1]);'
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);'
            'print(json.dumps(module.check_imports(json.loads(sys.argv[2]))))')
    return json.loads(checked([profile['python']['path'], '-B', '-c', code, validator,
                               json.dumps({'imports': imports})], env=python_env(profile)))


def pkg_config_probe(profile):
    """Resolve the bwrap crate's actual libcap inputs without compiling anything."""
    config = profile['native_build'].get('pkg_config')
    env = python_env(profile)
    if config is not None:
        env['PKG_CONFIG_PATH'] = config['path']
    program = str(Path('/usr/bin/pkg-config').resolve())
    pc = Path(checked([program, '--path', 'libcap'], env=env).strip())
    include = Path(checked([program, '--variable=includedir', 'libcap'], env=env).strip())
    library = Path(checked([program, '--variable=libdir', 'libcap'], env=env).strip()) / 'libcap.so'
    protocol.require(all(path.is_absolute() for path in (pc, include, library)), 'Relative libcap build input refused')
    required = {str(path) for path in (pc, include / 'sys/capability.h', library)}
    declared = config['files'] if config is not None else {}
    protocol.require(config is None or required <= set(declared), 'Pin the resolved libcap pc, header and linker input')
    if config is not None:
        protocol.require(pc.resolve().is_relative_to(Path(config['path']).resolve()), 'Wrong libcap pkg-config origin')
    inputs = {}
    for name in sorted(required | set(declared)):
        path = Path(name)
        resolved = path.resolve(strict=True)
        record = protocol.file_record(resolved)
        protocol.require(config is None or declared[name] == record['sha256'], f'libcap input hash mismatch: {name}')
        inputs[name] = dict(record, resolved=str(resolved), link=os.readlink(path) if path.is_symlink() else None)
    with library.open('rb') as stream:
        header = stream.read(20)
    protocol.require(header[:6] == b'\x7fELF\x02\x01' and int.from_bytes(header[18:20], 'little') ==
                     {'x86_64': 62, 'aarch64': 183}[profile['architecture']], 'libcap linker input is not native ELF')
    return {'pkg_config_path': config['path'] if config is not None else None,
            'version': checked([program, '--modversion', 'libcap'], env=env).strip(),
            'cflags': checked([program, '--cflags', 'libcap'], env=env).strip(),
            'libs': checked([program, '--libs', 'libcap'], env=env).strip(), 'inputs': inputs}


def checked(argv, *, cwd=None, env=None):
    return subprocess.check_output(list(map(str, argv)), cwd=cwd, env=env,
                                   stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT, text=True)


def checkout_identity(root):
    """Every Git operation uses a disposable index, including cleanliness checks."""
    root = Path(root).resolve()
    index = Path(checked(['/usr/bin/git', '-C', root, 'rev-parse', '--git-path', 'index']).strip())
    if not index.is_absolute():
        index = root / index
    git_dir = Path(checked(['/usr/bin/git', '-C', root, 'rev-parse', '--absolute-git-dir']).strip())
    preserved = {path for path in (index, git_dir / 'index', git_dir / 'HEAD', *git_dir.glob('sharedindex.*'))
                 if path.is_file()}
    git_metadata = {str(path): dict(protocol.file_record(path), inode=path.stat().st_ino,
                                    mtime_ns=path.stat().st_mtime_ns) for path in preserved}
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    with tempfile.TemporaryDirectory(prefix='release-index-') as temporary:
        scratch = Path(temporary) / 'index'
        scratch.write_bytes(before[0])
        env = os.environ | {'GIT_INDEX_FILE': str(scratch), 'GIT_OPTIONAL_LOCKS': '0'}
        head = checked(['/usr/bin/git', '-C', root, 'rev-parse', 'HEAD'], env=env).strip()
        protocol.require(not checked(['/usr/bin/git', '-C', root, 'status', '--porcelain',
                                      '--untracked-files=normal'], env=env).strip(),
                         f'Pinned checkout is dirty: {root}')
        names = checked(['/usr/bin/git', '-C', root, 'ls-files', '-z'], env=env).split('\0')
    protocol.require(before == (index.read_bytes(), index.stat().st_mtime_ns), 'Original Git index changed')
    protocol.require(git_metadata == {str(path): dict(protocol.file_record(path), inode=path.stat().st_ino,
                       mtime_ns=path.stat().st_mtime_ns) for path in preserved}, 'Original Git metadata changed')
    inventory = {}
    for name in filter(None, names):
        path = root / name
        info = path.lstat()
        row = {'inode': info.st_ino, 'device': info.st_dev, 'mode': info.st_mode,
               'size': info.st_size, 'mtime_ns': info.st_mtime_ns}
        if path.is_symlink():
            row['link'] = os.readlink(path)
        else:
            row['sha256'] = sha(path)
        inventory[name] = row
    return {'root': str(root), 'head': head, 'index': str(index),
            'index_sha256': hashlib.sha256(before[0]).hexdigest(),
            'index_mtime_ns': before[1], 'git_metadata': git_metadata, 'files': inventory}


def observe(profile):
    """Probe the exact interpreter, loader, imports and Rust binaries used below."""
    env = python_env(profile)
    probe = ('import json,sys,_json;from pathlib import Path;'
             'libraries=sorted({line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines() '
             'if "libpython" in line and line.split()[-1].startswith("/")});'
             'print(json.dumps(dict(version=sys.version.split()[0],'
             'libraries=libraries,json_extension=_json.__file__,executable=sys.executable)))')
    python = json.loads(checked([profile['python']['path'], '-B', '-c', probe], env=env))
    protocol.require(python['version'] == profile['python']['version'], 'Python version mismatch')
    expected = profile['python']['libpython']
    protocol.require((not python['libraries']) if expected is None else
                     {str(Path(p).resolve()) for p in python['libraries']} == {str(Path(expected).resolve())},
                     'Python loader/libpython mismatch')
    python['binary_sha256'] = sha(Path(profile['python']['path']).resolve())
    python['json_extension_sha256'] = sha(Path(python['json_extension']).resolve())
    python['libpython_sha256'] = sha(Path(expected).resolve()) if expected else None
    python['fixture_imports'] = fixture_import_probe(profile)
    rust = checked([profile['toolchain']['rustc'], '--version'], env=env).strip()
    cargo = checked([profile['toolchain']['cargo'], '--version'], env=env).strip()
    version = profile['toolchain']['version']
    protocol.require(rust.split()[1] == version and cargo.split()[1] == version, 'Rust toolchain mismatch')
    result = {'hostname': platform.node(), 'architecture': platform.machine(),
              'toolchain': profile['toolchain'], 'python': profile['python'],
              'executor_package_root': str(Path(profile['executor_package_root']).resolve()),
              'tooling_commit': checkout_identity(profile['tooling_root'])['head'],
              'plus_source_commit': checkout_identity(profile['source_root'])['head']}
    protocol.validate_profile(profile, result['architecture'], result)
    return result, python


def _hook(spec, root, report):
    protocol.keys(spec, ('argv', 'cwd', 'env', 'deadline_seconds'))
    protocol.require(isinstance(spec['argv'], list) and bool(spec['argv']) and
                     Path(spec['argv'][0]).is_absolute() and Path(spec['cwd']).is_absolute() and
                     isinstance(spec['env'], dict) and type(spec['deadline_seconds']) is int and
                     0 < spec['deadline_seconds'] <= 300, 'Invalid cleanup hook')
    substitutions = {'{attempt}': str(root), '{report}': str(report)}
    argv = []
    for value in spec['argv']:
        for key, replacement in substitutions.items():
            value = value.replace(key, replacement)
        argv.append(value)
    return dict(spec, argv=argv)


def input_closure(profile):
    paths = [Path(__file__).resolve(), Path(protocol.__file__).resolve(),
             *sorted(path for path in RECIPE.iterdir() if path.is_file()),
             Path(profile['python']['path']), Path(profile['toolchain']['rustc']),
             Path(profile['toolchain']['cargo']), Path(profile['executor_package_root']) / 'bin/codex-plus-runtime']
    # Imported fixtures are execution input too, even when not the entrypoint.
    source = Path(profile['source_root'])
    paths.append(source / 'bin/codex-plus')
    paths += sorted((source / 'tests').rglob('*.py'))
    paths += sorted((source / 'codex_plus').rglob('*.py'))
    paths += sorted((source / 'scripts').glob('*.py'))
    paths.append(Path(profile['native_build']['official_binary']))
    paths += [Path(pin['origin']) for pin in profile['python'].get('fixture_imports', {}).values()]
    paths += [Path(path) for path in profile.get('system_tools', {})]
    return {str(path.resolve()): protocol.file_record(path.resolve()) for path in paths}


def prepare(root, cleanup, *, rehearsal_failure=None):
    """Freeze local inputs and emit a proposal. This does not prepare/launch a job."""
    root = Path(root).resolve()
    protocol.require_native_attempt(root)
    metadata, plan, profile = protocol.read_attempt(root)
    protocol.require(plan['upstream']['version'] == '0.163.0', 'Upstream recipe needs qualification')
    protocol.require(profile['toolchain']['version'] == '1.99.1', 'Compiler recipe needs qualification')
    protocol.require(Path(profile['tooling_root']).resolve() == Path(__file__).resolve().parents[1],
                     'Run from the pinned tooling checkout')
    protocol.require(rehearsal_failure is None or metadata['rehearsal'] and rehearsal_failure in STAGES,
                     'Failure injection is rehearsal-only')
    build = profile['native_build']
    protocol.keys(build, NATIVE_KEYS, ('pkg_config',))
    for name in ('upstream_repository', 'cargo_home', 'cargo_target_dir', 'v8_cache',
                 'license_cache', 'official_binary'):
        protocol.require(Path(build[name]).is_absolute() and Path(build[name]).exists(), f'Missing native input: {name}')
    for name in ('cargo_jobs', 'test_threads'):
        protocol.require(type(build[name]) is int and build[name] > 0, f'Invalid {name}')
    for path in SYSTEM_TOOLS:
        canonical = str(Path(path).resolve())
        protocol.require(profile.get('system_tools', {}).get(canonical) == sha(canonical),
                         f'System tool needs an explicit hash pin: {canonical}')
    protocol.require(sha(build['official_binary']) == build['official_sha256'], 'Official binary mismatch')
    observed, python = observe(profile)
    protocol.require(observed == metadata['native'], 'Native identity changed since init')
    pkg_config = pkg_config_probe(profile) if not metadata['rehearsal'] or 'pkg_config' in build else None
    _hook(cleanup, root, root / 'outputs/job-cleanup.json')
    ownership_path = str((Path(profile['tooling_root']) / 'scripts/release_ownership.py').resolve())
    common_hook = ownership_path in cleanup['argv'] and 'audit' in cleanup['argv']
    protocol.require(common_hook or metadata['rehearsal'], 'Production requires the pinned common ownership audit')
    final_cleanup = dict(cleanup, argv=[value if value != 'audit' else 'cleanup' for value in cleanup['argv']])
    ownership = module(ownership_path)
    baseline = ownership.capture_baseline([root, Path(metadata['socket_root'])], scope=root)
    protocol.write_once(root / 'ownership-baseline.json', baseline)
    closure = input_closure(profile)
    fixture_files = fixture_closure(profile)
    for name in cleanup['argv']:
        if Path(name).is_absolute() and Path(name).is_file():
            closure[str(Path(name).resolve())] = protocol.file_record(Path(name).resolve())
    protocol._executed(profile, list(closure))
    sealed = {name: checkout_identity(profile[name]) for name in ('source_root', 'tooling_root')}
    context = {'schema_version': 1, 'attempt': metadata['attempt'], 'plan_sha256': protocol.digest(plan),
               'profile_sha256': protocol.digest(profile), 'closure': closure,
               'seals': sealed, 'python_probe': python, 'cleanup': cleanup, 'final_cleanup': final_cleanup,
               'fixture_closure': fixture_files,
               'pkg_config': pkg_config,
               'ownership_baseline_sha256': sha(root / 'ownership-baseline.json'),
               'rehearsal_failure': rehearsal_failure}
    protocol.write_once(root / 'native-preparation.json', context)
    # The controller is a control ancestor; only stage children carry ownership.
    env = python_env(profile)
    script = str(Path(__file__).resolve())
    proposal = {'schema': 1, 'topic': 'Qualify local native release',
                'argv': [profile['python']['path'], '-B', script, 'run', '--attempt', str(root),
                         '--execution', '{execution}'], 'cwd': profile['tooling_root'], 'env': env,
                'inputs': [{'source': str(root / name), 'path': name} for name in
                           ('attempt.json', 'plan.json', 'profile.json', 'native-preparation.json', 'ownership-baseline.json')],
                'exclusive_keys': ['release-' + metadata['architecture'] + '-' + profile['host_id'],
                                   'cargo-' + protocol.digest(build['cargo_target_dir'])[:24]],
                'expected_duration_seconds': 120 if metadata['rehearsal'] else 7200,
                'deadline_seconds': 600 if metadata['rehearsal'] else 18000,
                'cancel_grace_seconds': 30 if metadata['rehearsal'] else 90,
                'external_dependencies': [{'identity': 'Pinned local source, tooling, compiler, Python, installed executor and caches',
                    'precondition': {'argv': [profile['python']['path'], '-B', script, 'verify', '--attempt', str(root)],
                                     'cwd': profile['tooling_root'], 'env': env, 'deadline_seconds': 180}}],
                'cleanup': {'argv': [profile['python']['path'], '-B', script, 'cleanup', '--attempt', str(root)],
                            'cwd': profile['tooling_root'], 'env': env, 'deadline_seconds': 300}}
    # Freeze all entrypoints, imported fixtures and tools through the normal executor.
    proposal['inputs'] += [{'source': name, 'path': 'closure/' + str(number)}
                           for number, name in enumerate(context['closure'])
                           if Path(name).suffix == '.py' or Path(name).parent == RECIPE]
    proposal['inputs'] += [{'source': name, 'path': 'fixture/' + str(number)}
                           for number, name in enumerate(fixture_files) if name not in context['closure']]
    if pkg_config is not None:
        proposal['inputs'] += [{'source': record['resolved'], 'path': 'pkg-config/' + str(number)}
                               for number, record in enumerate(pkg_config['inputs'].values())]
    protocol.write_once(root / 'job-proposal.json', proposal)
    return proposal


def verify(root):
    protocol.require_native_attempt(root)
    metadata, plan, profile = protocol.read_attempt(root)
    context = protocol.load(Path(root) / 'native-preparation.json')
    protocol.require(context['attempt'] == metadata['attempt'] and context['plan_sha256'] == protocol.digest(plan)
                     and context['profile_sha256'] == protocol.digest(profile), 'Native preparation binding changed')
    protocol.require(all(protocol.file_record(name) == record for name, record in context['closure'].items()),
                     'Native input closure changed')
    protocol.require(sha(Path(root) / 'ownership-baseline.json') == context['ownership_baseline_sha256'],
                     'Ownership baseline changed')
    protocol.require(fixture_closure(profile) == context['fixture_closure'], 'Fixture dependency closure changed')
    if context['pkg_config'] is not None:
        protocol.require(pkg_config_probe(profile) == context['pkg_config'], 'libcap build inputs changed')
    for name, seal in context['seals'].items():
        protocol.require(seal == checkout_identity(profile[name]), f'Source seal changed: {name}')
    observed, probe = observe(profile)
    protocol.require(observed == metadata['native'] and probe == context['python_probe'], 'Native runtime changed')
    return context


def journal(root, event, identity, argv, exit_code=None):
    with (Path(root) / 'process-journal.jsonl').open('ab') as stream:
        stream.write(protocol.encoded({'event': event, 'identity': identity, 'argv': list(map(str, argv)),
                                       'exit_code': exit_code, 'at_ns': time.time_ns()}))
        stream.flush()
        os.fsync(stream.fileno())


def process_identity(pid):
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pid': pid, 'start_ticks': int(fields[19])}


def owned_run(argv, *, helper, python, cwd, env, stdout, timeout):
    """Use the fleet-pinned native owned-child subreaper, including readiness ack."""
    reader, writer = socket.socketpair()
    guard = None
    identity = None
    try:
        with open('/dev/null', 'rb') as input_stream:
            trampoline = ('import os,sys; fd=int(sys.argv[1]); os.dup2(fd,3,inheritable=True); '
                          'os.close(fd) if fd!=3 else None; os.execv(sys.argv[2],sys.argv[2:])')
            guard_argv = [str(python), '-B', '-c', trampoline, str(input_stream.fileno()),
                          str(helper), 'advisor', '--owned-child', '--', *map(str, argv)]
            guard = subprocess.Popen(guard_argv,
                                     cwd=cwd, env=env, stdin=reader, stdout=stdout, stderr=subprocess.STDOUT,
                                     pass_fds=(input_stream.fileno(),), start_new_session=True)
        identity = process_identity(guard.pid)
        journal(cwd, 'started', identity, guard_argv)
        reader.close()
        writer.settimeout(5)
        protocol.require(writer.recv(1) == b'\x01', 'Owned supervisor did not acknowledge readiness')
        return guard.wait(timeout=timeout)
    finally:
        reader.close()
        writer.close()
        if guard is not None and guard.poll() is None:
            # Closing the control socket asks the existing supervisor to reap its scope.
            guard.wait(timeout=8)
        if identity is not None and guard.poll() is not None:
            journal(cwd, 'finished', identity, guard_argv, guard.returncode)


def cleanup(root, report=None, *, final=False):
    root = Path(root).resolve()
    protocol.require_native_attempt(root)
    context = protocol.load(root / 'native-preparation.json')
    report = Path(report) if report is not None else root / 'outputs/job-cleanup.json'
    protocol.require(not report.exists(), 'Preserve existing cleanup outcome')
    command = _hook(context['final_cleanup'] if final else context['cleanup'], root, report)
    with report.with_suffix('.log').open('xb') as stream:
        result = subprocess.run(command['argv'], cwd=command['cwd'], env=command['env'],
                                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                timeout=command['deadline_seconds'])
    protocol.require(report.is_file(), 'Cleanup hook did not write evidence')
    evidence = protocol.load(report)
    protocol.require(isinstance(evidence, dict), 'Cleanup hook evidence must be an object')
    protocol.require(result.returncode == 0 and evidence.get('outcome') == 'clean' and
                     evidence.get('owned_remaining') == [] and evidence.get('unknown') == [],
                     f'Cleanup unresolved: {report}')
    verify(root)
    return {'outcome': 'clean', 'owned_remaining': 0, 'unknown': 0,
            'report': report.relative_to(root).as_posix()}


def execution_identity(directory, root, profile):
    directory = Path(directory).resolve()
    record = protocol.load(directory / 'state.json')
    helper = Path(profile['executor_package_root']) / 'bin/codex-plus-runtime'
    protocol.require(record['id'] == directory.name and record['state'] == 'running' and
                     Path(record['package']).resolve() == Path(profile['executor_package_root']).resolve() and
                     record['runtime']['sha256'] == sha(helper), 'Managed execution identity mismatch')
    workload = record.get('workload') or {}
    protocol.require(all(workload.get(key) == value for key, value in process_identity(os.getpid()).items()),
                     'Managed workload does not own this process')
    command = record['manifest']['command']['spec']
    protocol.require(command['argv'][2:] == [str(Path(__file__).resolve()), 'run', '--attempt', str(root),
                                            '--execution', str(directory)], 'Managed command binding mismatch')
    return record['id'], sha(helper)


def run(root, execution):
    root = Path(root).resolve()
    protocol.require_native_attempt(root)
    metadata, plan, profile = protocol.read_attempt(root)
    context = verify(root)
    state = protocol.status(root)
    protocol.require(not state['refused'], 'Failed/unknown receipt is immutable; review and create a new attempt')
    protocol.require(not (root / 'socket-cleaned.json').exists(), 'Attempt cleanup is final; do not recreate fixture ownership')
    job_id, executor_sha = execution_identity(execution, root, profile)
    for name in ('logs', 'native', 'packages', 'extracted', 'evidence', 'home', 'control-home'):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    socket_root = Path(metadata['socket_root'])
    socket_root.mkdir(mode=0o700, exist_ok=True)
    protocol.require(socket_root.stat().st_mode & 0o777 == 0o700, 'Fixture root must be private')
    for stage_name in STAGES:
        if stage_name in state['receipts']:
            continue
        start_file = root / 'native-starts' / (stage_name + '.json')
        protocol.require(not start_file.exists(), 'Interrupted stage has unknown outcome; do not replay')
        protocol.write_once(start_file, {'job_id': job_id, 'stage': stage_name, 'started_ns': time.time_ns()})
        argv = [profile['python']['path'], '-B', str(Path(__file__).resolve()), 'stage',
                '--attempt', str(root), '--stage', stage_name]
        env = Context(root, stage_name).ENV
        log = root / 'logs' / (stage_name + '.log')
        outcome, code = 'unknown', None
        error = None
        try:
            with log.open('xb') as stream:
                code = owned_run(argv, helper=Path(profile['executor_package_root']) / 'bin/codex-plus-runtime',
                                 python=profile['python']['path'], cwd=root, env=env, stdout=stream, timeout=14400)
            outcome = 'passed' if code == 0 else 'failed'
        except Exception as exc:
            error = repr(exc)
        cleanup_report = root / 'outputs' / (stage_name + '-cleanup.json')
        try:
            cleanup_result = cleanup(root, cleanup_report)
        except Exception as exc:
            failure_report = cleanup_report.with_name(cleanup_report.stem + '-failure.json')
            try:
                raw_cleanup = protocol.load(cleanup_report)
            except (OSError, ValueError):
                raw_cleanup = {}
            if not isinstance(raw_cleanup, dict):
                raw_cleanup = {}
            counts = {}
            for key in ('owned_remaining', 'unknown'):
                value = raw_cleanup.get(key)
                counts[key] = len(value) if isinstance(value, list) else value if type(value) is int and value >= 0 else 0
            counts['unknown'] = max(1, counts['unknown'])
            protocol.write_once(failure_report, {'outcome': 'unknown', **counts, 'error': repr(exc),
                'raw_report': cleanup_report.relative_to(root).as_posix() if cleanup_report.exists() else None})
            cleanup_result = {'outcome': 'unknown', **counts,
                              'report': failure_report.relative_to(root).as_posix()}
        if (root / 'process-journal.jsonl').exists():
            protocol.write_once(root / 'outputs' / (stage_name + '-processes.json'),
                                [json.loads(line) for line in (root / 'process-journal.jsonl').read_text().splitlines()])
        try:
            verify(root)
        except Exception as exc:
            outcome, error = 'failed', repr(exc)
        evidence = root / 'outputs' / (stage_name + '.json')
        if not evidence.exists():
            protocol.write_once(evidence, {'architecture': metadata['architecture'],
                'plus_source_commit': plan['plus_source_commit'], 'error': error or 'Stage produced no report'})
            outcome = 'failed' if code is not None else 'unknown'
        executed = set(context['closure'])
        for path in (root / 'evidence/commands').glob('*.json'):
            record = protocol.load(path)
            if record.get('executable'):
                executed.add(record['executable'])
        if stage_name in ('package', 'native-tests'):
            package_name = 'codex-plus-' + plan['release'] + '-' + profile['triple']
            packaged = root / 'extracted' / package_name
            for name in ('bin/codex', 'bin/codex-code-mode-host', 'bin/codex-plus-runtime',
                         'bin/codex-plus', 'codex-resources/bwrap'):
                if (packaged / name).exists():
                    executed.add(str(packaged / name))
        compiler_artifacts = None
        artifact_path = root / 'outputs/native-tests-compiler-artifacts.json'
        if stage_name == 'native-tests' and artifact_path.exists():
            compiler_artifacts = artifact_path.relative_to(root).as_posix()
        inputs = ['attempt.json', 'plan.json', 'profile.json', 'native-preparation.json']
        if (root / 'ownership-baseline.json').exists():
            inputs.append('ownership-baseline.json')
        if compiler_artifacts:
            inputs.append(compiler_artifacts)
        outputs = sorted(path.relative_to(root).as_posix() for parent in ('outputs', 'logs', 'evidence',
                          'native', 'packages', 'extracted', 'test-binaries') for path in (root / parent).rglob('*')
                         if path.is_file() and not path.is_symlink())
        protocol.write_receipt(root, stage_name, inputs=inputs, executed=sorted(executed), outputs=outputs,
                               evidence=evidence.relative_to(root).as_posix(),
                               execution={'job_id': job_id, 'executor_package_root': profile['executor_package_root'],
                                          'executor_sha256': executor_sha, 'architecture': metadata['architecture'],
                                          'plus_source_commit': plan['plus_source_commit'],
                                          'tooling_commit': plan['tooling_commit'], 'outcome': outcome,
                                          'exit_code': code, 'argv': argv, 'cwd': str(root), 'env': env},
                               cleanup=cleanup_result, compiler_artifacts=compiler_artifacts)
        protocol.require(outcome == 'passed' and cleanup_result['outcome'] == 'clean',
                         f'Stage {stage_name} {outcome}; preserve logs and receipts, no in-place retry')
    return protocol.status(root)


class Context:
    """The explicit data/context consumed by the accepted native recipe."""
    def __init__(self, root, stage_name):
        self.BASE = Path(root).resolve()
        metadata, self.plan, self.profile = protocol.read_attempt(root)
        build = self.profile['native_build']
        self.stage_name = stage_name
        self.SOURCE = Path(self.profile['source_root'])
        self.INPUTS = RECIPE
        self.UPSTREAM = self.BASE / 'upstream'
        self.UPSTREAM_REPOSITORY = Path(build['upstream_repository'])
        self.NATIVE = self.BASE / 'native'
        self.TARGET = self.profile['triple']
        self.PYTHON = self.profile['python']['path']
        self.CARGO = Path(self.profile['toolchain']['cargo'])
        self.CARGO_TARGET = Path(build['cargo_target_dir'])
        self.V8_CACHE = Path(build['v8_cache'])
        self.JOBS, self.TEST_THREADS = build['cargo_jobs'], build['test_threads']
        self.OFFICIAL = Path(build['official_binary'])
        name = 'codex-plus-' + self.plan['release'] + '-' + self.TARGET
        self.PACKAGE = self.BASE / 'extracted' / name
        self.ARCHIVE = self.BASE / 'packages' / (name + '.tar.gz')
        self.SHORT = type('ShortRoot', (), {'name': metadata['socket_root']})()
        self.ENV = python_env(self.profile) | {'HOME': str(self.BASE / 'home'),
            'PATH': str(RECIPE) + ':/usr/bin:/bin',
            'CODEX_HOME': str(self.BASE / 'control-home'), 'TMPDIR': self.SHORT.name,
            'RUSTC': self.profile['toolchain']['rustc'], 'RUSTUP_AUTO_INSTALL': '0',
            'CODEX_PLUS_RELEASE_CARGO': str(self.CARGO),
            'CODEX_PLUS_RELEASE_RUST_VERSION': self.profile['toolchain']['version'],
            'CARGO_HOME': build['cargo_home'], 'CARGO_TARGET_DIR': build['cargo_target_dir'],
            'CARGO_NET_OFFLINE': 'true', 'CARGO_BUILD_TARGET': self.TARGET, 'CARGO_BUILD_JOBS': str(self.JOBS),
            'RUST_TEST_THREADS': str(self.TEST_THREADS), 'RUST_MIN_STACK': '33554432',
            'CODEX_REPO_ROOT': str(self.UPSTREAM), 'XDG_CACHE_HOME': str(self.V8_CACHE.parent),
            'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
            'CODEX_PLUS_TEST_RUNTIME': str(self.NATIVE / 'codex-plus-runtime'),
            'CODEX_PLUS_RELEASE_QA_SCOPE': str(self.BASE), 'CODEX_PLUS_RELEASE_ATTEMPT': metadata['attempt'],
            'GIT_OPTIONAL_LOCKS': '0', 'INSTA_UPDATE': 'no'}
        if (self.PACKAGE / 'bin/codex-plus-runtime').exists():
            self.ENV['CODEX_PLUS_TEST_RUNTIME'] = str(self.PACKAGE / 'bin/codex-plus-runtime')
        if (self.NATIVE / 'bwrap').exists():
            self.ENV['CODEX_BWRAP_SHA256'] = sha(self.NATIVE / 'bwrap')
        if 'pkg_config' in build:
            self.ENV['PKG_CONFIG_PATH'] = build['pkg_config']['path']
        previous = self.BASE / 'evidence/frontend-detail.json'
        if previous.exists():
            self.ENV.update({key: value['path'] for key, value in protocol.load(previous).get('v8', {}).items()})
        self.REPORT = {'source_commit': self.plan['plus_source_commit'],
                       'frontend_patch_sha256': self.plan['patch']['sha256'], 'checks': {}}

    digest = staticmethod(sha)

    def run(self, name, argv, *, cwd=None, timeout=180):
        argv = list(map(str, argv))
        if argv[0] == self.PYTHON and argv[1:2] != ['-B']:
            argv.insert(1, '-B')
        path = self.BASE / 'logs' / (name + '.log')
        start = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        record = {'argv': argv, 'cwd': str(cwd or self.BASE), 'exit_code': None,
                  'executable': str(Path(argv[0]).resolve()), 'executable_sha256': sha(Path(argv[0]).resolve())}
        protocol.write_once(self.BASE / 'evidence/commands' / (name + '.started.json'), record)
        try:
            with path.open('xb') as stream:
                result = subprocess.run(argv, cwd=cwd or self.BASE, env=self.ENV, stdin=subprocess.DEVNULL,
                                        stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
            record['exit_code'] = result.returncode
        finally:
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            record.update(seconds=time.monotonic() - start, user_seconds=after.ru_utime - before.ru_utime,
                          system_seconds=after.ru_stime - before.ru_stime, log=str(path),
                          log_sha256=sha(path) if path.exists() else None)
            protocol.write_once(self.BASE / 'evidence/commands' / (name + '.json'), record)
            self.REPORT['checks'][name] = record
        protocol.require(result.returncode == 0, f'{name} failed ({result.returncode}); {path}')
        return path.read_text(errors='replace')

    def record_harness(self, artifact, saved):
        proof = {'architecture': self.profile['architecture'], 'plus_source_commit': self.plan['plus_source_commit'],
                 'tooling_commit': self.plan['tooling_commit'], 'target': self.TARGET,
                 'source_sha256': protocol.digest({name: self.plan[name] for name in
                                                   ('upstream', 'patch', 'plus_source_commit')}),
                 'compiler_sha256': sha(Path(self.profile['toolchain']['rustc']).resolve()),
                 'files': {path.relative_to(self.BASE).as_posix(): protocol.file_record(path)
                           for path in (saved, saved.with_name('codex-plus-runtime'))},
                 'cargo_artifact': artifact}
        protocol.write_once(self.BASE / 'outputs/native-tests-compiler-artifacts.json', proof)

    def install_native(self, name):
        shutil.copy2(self.CARGO_TARGET / self.TARGET / 'release' / name, self.NATIVE / name)
        self.run('strip-' + name, ['/usr/bin/strip', '--strip-unneeded', self.NATIVE / name])

    def cargo_timing(self, name):
        shutil.copyfile(self.CARGO_TARGET / 'cargo-timings/cargo-timing.html', self.BASE / 'evidence' / (name + '.html'))

    def fixture(self, program, *args):
        code = ('import runpy,sys,tempfile; original=tempfile.TemporaryDirectory;'
                'tempfile.TemporaryDirectory=lambda *a,**kw: original(prefix="",dir=' + repr(self.SHORT.name) +
                ') if not a and "dir" not in kw else original(*a,**kw);'
                'sys.path.insert(0,' + repr(str(self.SOURCE / 'tests')) + ');sys.argv=' +
                repr([str(self.SOURCE / 'tests' / program), *map(str, args)]) +
                ';runpy.run_path(sys.argv[0],run_name="__main__")')
        return [self.PYTHON, '-B', '-c', code]


def stage(root, name):
    metadata, plan, profile = protocol.read_attempt(root)
    context = verify(root)
    c = Context(root, name)
    report = {'architecture': metadata['architecture'], 'plus_source_commit': plan['plus_source_commit']}
    if metadata['rehearsal']:
        protocol.require(context['rehearsal_failure'] != name, 'Requested rehearsal failure')
        report['rehearsal'] = True
    if name == 'preflight':
        selected = [('test_claude_advisor', ['ClaudeAdvisor']),
                    ('test_claude_advisor_stdio', ['WorkerPipes']),
                    ('test_executor_reconcile_startup', ['case', 'main']),
                    ('fixtures.detached_jobs', ['DetachedJobServer']),
                    ('test_release_package', ['main', 'private_env', 'resources_check', 'exec_check']),
                    ('fixtures.release_package_provider', ['PackageProvider']),
                    ('native_smoke', ['Terminal', 'run']), ('test_morandi_tui', ['ThemeServer', 'run']),
                    ('test_command_display_tui', ['run', 'run_case']),
                    ('test_morandi_command_colors_tui', ['run']), ('test_shell_policy_integration', ['run']),
                    ('test_shell_topics_tui', ['run']), ('test_detached_execution_hud_tui', ['run'])]
        spec = c.BASE / 'outputs/import-preflight-input.json'
        dependency_imports = [{'module': name, 'origin': pin['origin'],
                               'symbols': ['Screen', 'Stream'] if name == 'pyte' else ['wcwidth']}
                              for name, pin in profile['python']['fixture_imports'].items()]
        protocol.write_once(spec, {'schema_version': 1,
            'imports': [{'module': module_name, 'origin': str(c.SOURCE / 'tests' / (module_name.replace('.', '/') + '.py')),
                         'symbols': symbols} for module_name, symbols in selected] + dependency_imports})
        c.run('import-preflight', [c.PYTHON, c.SOURCE / 'scripts/release-preflight.py', spec,
            '--scratch-parent', c.SHORT.name, '--output', c.BASE / 'evidence/import-preflight.json'], cwd=c.SOURCE)
        c.run('actual-fixture-setup', [c.PYTHON, RECIPE / 'fixture-preflight.py', '--source', c.SOURCE,
            '--short-root', c.SHORT.name, '--runtime', Path(profile['executor_package_root']) / 'bin/codex-plus-runtime',
            '--stock', c.OFFICIAL, '--evidence', c.BASE / 'evidence/preflight-fixtures'], timeout=90)
        c.run('command-fixture-selftest', c.fixture('test_command_display_tui.py', '--self-test'), cwd=c.SOURCE)
        report['checks'] = {'actual-fixture-setup': {'outcome': 'passed'}, 'command-fixture-selftest': {'outcome': 'passed'}}
    elif metadata['rehearsal']:
        report['checks'] = {'rehearsal': {'outcome': 'passed'}}
    elif name == 'frontend':
        module(RECIPE / 'build.py').frontend(c)
        report['checks'] = {'frontend': {'outcome': 'passed'}, 'bwrap': {'outcome': 'passed'}}
    elif name == 'runtime':
        c.run('helper-build', [c.CARGO, 'build', '--locked', '--offline', '--release', '--target', c.TARGET,
            '--jobs', str(c.JOBS), '--timings', '--manifest-path', c.SOURCE / 'rust-runtime/Cargo.toml',
            '--bin', 'codex-plus-runtime'], cwd=c.SOURCE, timeout=1800)
        c.install_native('codex-plus-runtime')
        c.cargo_timing('cargo-helper-build')
        for binary in ('codex', 'codex-code-mode-host', 'bwrap', 'codex-plus-runtime'):
            output = c.run('elf-' + binary, ['/usr/bin/readelf', '-h', c.NATIVE / binary])
            protocol.require(('AArch64' if metadata['architecture'] == 'aarch64' else 'Advanced Micro Devices X86-64')
                             in output, 'Wrong native ELF architecture')
            protocol.require('not found' not in c.run('ldd-' + binary, ['/usr/bin/ldd', c.NATIVE / binary]),
                             'Native loader dependency missing')
        protocol.require(c.run('codex-version', [c.NATIVE / 'codex', '--version']).strip() ==
                         'codex-cli ' + plan['upstream']['version'], 'Frontend version mismatch')
        report['checks'] = {'runtime': {'outcome': 'passed'}}
    elif name == 'package':
        package(c, report)
    elif name == 'native-tests':
        native_tests(c, report)
    detail = Path(root) / 'evidence' / (name + '-detail.json')
    protocol.write_once(detail, c.REPORT)
    for check in report.get('checks', {}).values():
        check['evidence'] = {detail.relative_to(Path(root)).as_posix(): sha(detail)}
    if name == 'package' and not metadata['rehearsal']:
        for check in report['checks'].values():
            for suffix in ('report.json', 'qualification.json'):
                path = 'evidence/package-seven/' + suffix
                check['evidence'][path] = sha(Path(root) / path)
    protocol.write_once(Path(root) / 'outputs' / (name + '.json'), report)
    return report


def package(c, report):
    shutil.copytree(c.profile['native_build']['license_cache'], c.BASE / 'packages/license-cache')
    glibc = c.run('glibc', ['/usr/bin/getconf', 'GNU_LIBC_VERSION']).strip().split()[1]
    protocol.require(glibc == c.plan['targets'][c.profile['architecture']]['build_glibc'], 'Build glibc mismatch')
    # The existing packager runs git diff: protect the original product index too.
    seal = checkout_identity(c.SOURCE)
    with tempfile.TemporaryDirectory(prefix='release-package-index-') as temporary:
        scratch = Path(temporary) / 'index'
        shutil.copyfile(seal['index'], scratch)
        c.ENV['GIT_INDEX_FILE'] = str(scratch)
        try:
            c.run('package-build', [c.PYTHON, c.SOURCE / 'scripts/package-release.py', '--source-dir', c.UPSTREAM,
                '--native-dir', c.NATIVE, '--target', c.TARGET, '--build-glibc', glibc,
                '--plus-commit', c.plan['plus_source_commit'], '--output-dir', c.BASE / 'packages'],
                cwd=c.SOURCE, timeout=1200)
        finally:
            c.ENV.pop('GIT_INDEX_FILE', None)
    protocol.require(checkout_identity(c.SOURCE) == seal, 'Packaging changed source/index seal')
    spec = c.BASE / 'outputs/archive-preflight-input.json'
    protocol.write_once(spec, {'schema_version': 1, 'archive': {'path': str(c.ARCHIVE), 'sha256': sha(c.ARCHIVE)}})
    c.run('archive-preflight', [c.PYTHON, c.SOURCE / 'scripts/release-preflight.py', spec,
                              '--scratch-parent', c.SHORT.name, '--output', c.BASE / 'evidence/archive-preflight.json'], cwd=c.SOURCE)
    c.run('extract-package', ['/usr/bin/tar', '--same-permissions', '-xzf', c.ARCHIVE, '-C', c.BASE / 'extracted'])
    c.run('package-verify', [c.PYTHON, c.SOURCE / 'scripts/package-release.py', '--verify', c.PACKAGE], cwd=c.SOURCE)
    c.run('packaged-seven', [c.PYTHON, RECIPE / 'package-checks.py', '--source', c.SOURCE,
        '--package', c.PACKAGE, '--official', c.OFFICIAL, '--short-root', c.SHORT.name,
        '--evidence', c.BASE / 'evidence/package-seven'], cwd=c.SHORT.name, timeout=300)
    c.ENV['CODEX_PLUS_TEST_RUNTIME'] = str(c.PACKAGE / 'bin/codex-plus-runtime')
    module(RECIPE / 'build.py').terminals(c)
    c.run('public-probes', [c.PYTHON, RECIPE / 'operator-probes.py', '--command', c.PACKAGE / 'bin/codex-plus',
                           '--evidence', c.BASE / 'evidence/public-probes.json'], cwd=c.SOURCE)
    c.run('package-after-runtime-verify', [c.PYTHON, c.SOURCE / 'scripts/package-release.py', '--verify', c.PACKAGE],
          cwd=c.SOURCE)
    raw = protocol.load(c.BASE / 'evidence/package-seven/report.json')
    report['checks'] = {name: {'outcome': 'passed'} for name in raw['checks']}
    report['checks']['frontend_matrix'] = {'outcome': 'passed'}
    report['checks']['public_probes'] = {'outcome': 'passed'}
    report.update(archive=c.ARCHIVE.relative_to(c.BASE).as_posix(), package_dir=c.PACKAGE.relative_to(c.BASE).as_posix())
    protocol.require(set(report['checks']) == set(c.plan['gates'][c.profile['architecture']]['package']['checks']),
                     'Package gate recipe/plan differ; review recipe qualification')


def native_tests(c, report):
    common = ['--locked', '--offline', '--release', '--target', c.TARGET, '--jobs', str(c.JOBS),
              '--manifest-path', c.SOURCE / 'rust-runtime/Cargo.toml', '--lib']
    c.run('helper-unit-list', [c.CARGO, 'test', *common, '--', '--list'], cwd=c.SOURCE, timeout=1800)
    c.run('helper-units', [c.CARGO, 'test', *common, '--', '--test-threads=' + str(c.TEST_THREADS),
                          '--logfile', c.BASE / 'logs/helper-units.libtest'], cwd=c.SOURCE, timeout=600)
    build = module(RECIPE / 'build.py')
    build.harness(c)
    suites = {'runtime': {'listed': 'logs/helper-unit-list.log', 'stdout': 'logs/helper-units.log',
                          'logfile': 'logs/helper-units.libtest', 'exit_code': 0}}
    for name in build.FILTERS:
        label = name.replace(':', '_')
        suites[name] = {'listed': 'logs/tui-list-' + label + '.log', 'stdout': 'logs/tui-' + label + '.log',
                        'logfile': 'logs/tui-' + label + '.libtest', 'exit_code': 0}
    expected = c.plan['gates'][c.profile['architecture']]['native-tests']['suites']
    protocol.require(set(suites) == set(expected), 'Native suite recipe/plan differ; review qualification')
    validator = module(c.SOURCE / 'scripts/native-results.py')
    for name, suite in suites.items():
        result = validator.validate_test_output(name, (c.BASE / suite['listed']).read_text(),
            (c.BASE / suite['stdout']).read_text(), (c.BASE / suite['logfile']).read_text(),
            exit_code=suite['exit_code'], allowed_ignored=expected[name]['allowed_ignored'])
        protocol.require(result['passed'] == expected[name]['passed'], 'Native suite count changed')
    c.run('advisor-stdio', c.fixture('test_claude_advisor_stdio.py', '-v'), cwd=c.SOURCE, timeout=90)
    c.run('advisor-contracts', c.fixture('test_claude_advisor.py'), cwd=c.SOURCE, timeout=120)
    c.run('startup-three', c.fixture('test_executor_reconcile_startup.py', '--runtime', c.NATIVE / 'codex-plus-runtime',
        '--stock-binary', c.OFFICIAL, '--evidence', c.BASE / 'evidence/startup-three'), cwd=c.SOURCE)
    startup = protocol.load(c.BASE / 'evidence/startup-three/report.json')
    protocol.require(startup['passed'] and len(startup['cases']) == 3 and
                     all(row['passed'] and row['state'] == 'completed' and not row['live_executor_lock_probed']
                         for row in startup['cases']), 'Startup executor checks failed')
    report['suites'] = suites


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'prepare', 'verify', 'run', 'stage', 'cleanup', 'status'))
    parser.add_argument('--attempt', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--cleanup-hook', type=Path)
    parser.add_argument('--execution', type=Path)
    parser.add_argument('--stage', choices=STAGES)
    parser.add_argument('--rehearsal', action='store_true')
    parser.add_argument('--fail-stage', choices=STAGES)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.command == 'init':
        profile = protocol.load(args.profile)
        observed, _ = observe(profile)
        return {'attempt': str(protocol.init_attempt(protocol.load(args.plan), profile, profile['source_root'],
                                                     observed=observed, rehearsal=args.rehearsal))}
    protocol.require(args.attempt is not None, '--attempt is required')
    if args.command == 'prepare':
        protocol.require(args.cleanup_hook is not None, '--cleanup-hook is required')
        return prepare(args.attempt, protocol.load(args.cleanup_hook), rehearsal_failure=args.fail_stage)
    if args.command == 'run':
        protocol.require(args.execution is not None, '--execution is required')
        return run(args.attempt, args.execution)
    if args.command == 'stage':
        protocol.require(args.stage is not None, '--stage is required')
        return stage(args.attempt, args.stage)
    if args.command == 'verify':
        verify(args.attempt)
        return {'verified': True}
    if args.command == 'cleanup':
        return cleanup(args.attempt, final=True)
    return protocol.status(args.attempt)


if __name__ == '__main__':
    try:
        print(json.dumps(main(), indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
