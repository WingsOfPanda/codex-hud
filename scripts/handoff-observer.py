#!/usr/bin/env python3
"""Inspect, or explicitly replace, one root's detached coordinator helpers on Linux."""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import time
import uuid


sys.dont_write_bytecode = True
OPEN = {'ready', 'pending', 'starting', 'in_flight', 'uncertain',
        'cancel_requested', 'cancel_uncertain'}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def package_identity(root, verify=False):
    root = root.resolve(strict=True)
    manifest = json.loads((root / 'codex-plus.json').read_text())
    for name in ('codex_plus/background.py', 'codex_plus/ledger.py', 'codex_plus/rpc.py'):
        if name not in manifest['files']:
            raise ValueError(f'package lacks helper: {name}')
    inventory = manifest['files'] if verify else {
        name: manifest['files'][name] for name in
        ('codex_plus/background.py', 'codex_plus/ledger.py', 'codex_plus/rpc.py')}
    for name, expected in inventory.items():
        path = root / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or
                any(part.is_symlink() for part in (path, *path.parents) if part != root)):
            raise ValueError('unsafe package inventory')
        mode = int(expected['mode'], 8)
        if (not path.is_file() or path.stat().st_size != expected['size'] or
                digest(path) != expected['sha256'] or
                path.stat().st_mode & 0o777 not in (mode, mode & ~0o222)):
            raise ValueError(f'package inventory mismatch: {name}')
    if verify:
        actual = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}
        if actual != set(manifest['files']) | {'codex-plus.json'}:
            raise ValueError('package file inventory contains missing or unlisted files')
    return {'root': str(root), 'release': manifest['release'],
            'plus_commit': manifest['plus_commit'], 'manifest_sha256': digest(root / 'codex-plus.json')}


def process_identity(pid, parent, home, proc=Path('/proc')):
    """Return only exact module invocations; reject selected but foreign runtimes."""
    directory = proc / str(pid)
    command = (directory / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
    try:
        module = command.index(b'-m')
    except ValueError:
        return None
    if command[module + 1:module + 2] != [b'codex_plus.background']:
        return None
    if command[module + 2:module + 3] not in ([b'supervise'], [b'observe']):
        return None
    environment = dict(part.split(b'=', 1) for part in
                       (directory / 'environ').read_bytes().split(b'\0') if b'=' in part)
    # Capture only the fields needed for home, parent, and module identity.
    default_home = Path(os.fsdecode(environment.get(b'HOME', os.fsencode(Path.home())))) / '.codex'
    raw_home = environment.get(b'CODEX_HOME', os.fsencode(default_home))
    if Path(os.fsdecode(raw_home)).resolve() != home:
        return None
    selectors = [command[index + 1] for index, argument in enumerate(command[:-1])
                 if argument == b'--parent']
    selectors.extend(argument.split(b'=', 1)[1] for argument in command if argument.startswith(b'--parent='))
    if not selectors:
        selectors = [environment.get(b'CODEX_THREAD_ID')]
    if os.fsencode(parent) not in selectors:
        return None
    if (len(command) != 6 or command[1:3] != [b'-m', b'codex_plus.background'] or
            command[4:] != [b'--parent', os.fsencode(parent)]):
        raise ValueError(f'ambiguous selected helper argv: {pid}')
    try:
        cwd = (directory / 'cwd').resolve(strict=True)
        executable = (directory / 'exe').resolve(strict=True)
        if not executable.name.startswith('python'):
            raise ValueError(f'foreign selected helper interpreter: {pid}')
        pythonpath = os.fsdecode(environment.get(b'PYTHONPATH', b''))
        if pythonpath and Path(pythonpath.split(os.pathsep)[0]).resolve() != cwd:
            raise ValueError(f'foreign selected helper PYTHONPATH: {pid}')
        package = package_identity(cwd)
    except FileNotFoundError as exc:
        if not directory.exists():
            raise ProcessLookupError(pid) from exc
        raise ValueError(f'selected helper runtime is unavailable: {pid}') from exc
    stat = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    if command[3] == b'supervise' and int(stat[3]) != pid:
        raise ValueError(f'selected supervisor is not detached: {pid}')
    return {'pid': pid, 'start_ticks': stat[19], 'action': os.fsdecode(command[3]),
            'ppid': int(stat[1]), 'executable': str(executable),
            'cwd': str(cwd), 'pythonpath': pythonpath, 'package': package}


def helpers(parent, home):
    result = []
    for directory in Path('/proc').glob('[0-9]*'):
        try:
            found = process_identity(int(directory.name), parent, home)
            if found:
                result.append(found)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Other users' processes cannot be this user's coordinator.
            if directory.stat().st_uid == os.getuid():
                raise
    runtimes = {(row['cwd'], row['executable'], row['package']['manifest_sha256']) for row in result}
    if len(runtimes) > 1:
        raise ValueError('selected helpers have mixed runtimes; owner must inspect')
    return result


def ledger_connection(home, mode='ro'):
    path = home / 'codex-plus/background.sqlite'
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('symlink ledger refused')
    return sqlite3.connect(path.as_uri() + f'?mode={mode}', uri=True, timeout=3)


def snapshot(home, parent):
    if not (home / 'codex-plus/background.sqlite').exists():
        return {'parks': [], 'jobs': [], 'callbacks': []}
    with closing(ledger_connection(home)) as database:
        database.row_factory = sqlite3.Row
        database.execute('BEGIN')
        return {
            'parks': [dict(row) for row in database.execute(
                'SELECT * FROM parks WHERE parent=? ORDER BY id', (parent,))],
            'jobs': [dict(row) for row in database.execute(
                'SELECT jobs.* FROM jobs JOIN parks ON parks.id=jobs.park WHERE parks.parent=? ORDER BY jobs.id', (parent,))],
            'callbacks': [dict(row) for row in database.execute(
                'SELECT * FROM callbacks WHERE parent=? ORDER BY id', (parent,))],
        }


def pending(state, parent):
    return (any(row['parent'] == parent and row['state'] == 'armed' for row in state['parks']) or
            any(row['parent'] == parent and row['state'] in OPEN for row in state['callbacks']))


def retained(before, after):
    """Identity and frozen payloads stay fixed; ordinary delivery may advance."""
    keys = {'parks': ('id', 'parent', 'origin', 'reason', 'deadline'),
            'jobs': ('id', 'park', 'kind', 'thread', 'item', 'process'),
            'callbacks': ('id', 'parent', 'client', 'idempotent')}
    for table, fields in keys.items():
        indexed = {row['id']: row for row in after[table]}
        if table != 'callbacks' and set(indexed) != {row['id'] for row in before[table]}:
            raise ValueError(f'{table} identities changed: new registration or missing row')
        for old in before[table]:
            new = indexed.get(old['id'])
            if new is None or any(old[key] != new.get(key) for key in fields if key in old):
                raise ValueError(f'{table} identity changed: {old["id"]}')
            if table == 'jobs' and old.get('turn') is not None and old['turn'] != new.get('turn'):
                raise ValueError('known job turn changed')
            if table == 'callbacks' and old.get('message') is not None and old['message'] != new.get('message'):
                raise ValueError('frozen callback message changed')
            for field in ('state', 'status'):
                if old.get(field) == 'cancelled' and new.get(field) != 'cancelled':
                    raise ValueError('cancelled row was rearmed')


def health(home, parent):
    path = home / 'codex-plus' / f'{parent}.status.json'
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError('symlink health file refused')
    with path.open('rb') as stream:
        result = json.loads(stream.read(4097))
    if result.get('parent') != parent:
        raise ValueError('health parent mismatch')
    return result


def observer_lock_owner(home, parent):
    path = home / 'codex-plus' / f'{parent}.lock'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        identity = (os.major(metadata.st_dev), os.minor(metadata.st_dev), metadata.st_ino)
        owners = set()
        for line in Path('/proc/locks').read_text().splitlines():
            fields = line.split()
            if len(fields) != 8 or fields[1] != 'FLOCK':
                continue
            major, minor, inode = fields[5].split(':')
            if (int(major, 16), int(minor, 16), int(inode)) == identity:
                if fields[3] != 'WRITE':
                    raise ValueError('unexpected selected observer lock mode')
                owners.add(int(fields[4]))
        if len(owners) > 1:
            raise ValueError('ambiguous selected observer lock ownership')
        return next(iter(owners), None)
    finally:
        os.close(descriptor)


def root_idle(package, home, parent, generation=None):
    spec = importlib.util.spec_from_file_location('handoff_rpc', package / 'codex_plus/rpc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.Client(home / 'app-server-control/app-server-control.sock', timeout=3)
    try:
        thread = client.call('thread/read', {'threadId': parent, 'includeTurns': False})['thread']
        if (thread.get('id') != parent or thread.get('parentThreadId') or
                isinstance(thread.get('source'), dict) and 'subAgent' in thread['source'] or
                thread.get('status', {}).get('type') != 'idle'):
            raise ValueError('selected parent must be the loaded idle root; owner must quiesce it')
        if generation is not None and client.generation != generation:
            raise ValueError('daemon changed during handoff')
        return client.generation
    finally:
        client.close()


def stop_helper(identity, parent, home):
    pid = identity['pid']
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if process_identity(pid, parent, home) != identity:
            raise ValueError(f'helper identity changed before signal: {pid}')
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        if not select.select([descriptor], [], [], 5)[0]:
            raise ValueError(f'helper did not stop after SIGTERM: {pid}; no escalation performed')
    finally:
        os.close(descriptor)


def live_helper(pid, parent, home):
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return None
    try:
        identity = process_identity(pid, parent, home)
        if select.select([descriptor], [], [], .05 if identity is None else 0)[0]:
            return None
        if identity is None:
            raise ValueError('selected observer lock acquired by an unrecognized owner after launch')
        return identity
    except (FileNotFoundError, ProcessLookupError):
        return None
    finally:
        os.close(descriptor)


def save(directory, name, value):
    with (directory / name).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def handoff(args, before, old):
    package, home, parent = args.package, args.codex_home, args.parent
    identity = package_identity(package, verify=True)
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('Linux Python pidfd support required; no PID-only fallback')
    evidence = args.evidence_dir
    evidence.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {'phase': 'preflight', 'package': identity, 'old_helpers': old,
              'script_sha256': digest(Path(__file__)), 'stopped': []}
    child = None
    try:
        save(evidence, 'before.json', before)
        generation = root_idle(package, home, parent)
        owner = observer_lock_owner(home, parent)
        if owner is not None and not any(row['action'] == 'observe' and row['pid'] == owner for row in old):
            raise ValueError('selected observer lock has an unrecognized owner; no helpers stopped')
        report['old_observer_lock_owner'] = owner
        for action in ('supervise', 'observe'):
            # Rediscover after supervisor exit: an observer may have just been spawned.
            current = helpers(parent, home)
            for helper in current:
                if helper['action'] == action:
                    if not any(helper['cwd'] == known['cwd'] for known in old):
                        raise ValueError('new foreign helper appeared during handoff')
                    stop_helper(helper, parent, home)
                    report['stopped'].append(helper)
        if helpers(parent, home):
            raise ValueError('selected helper remains after stop')
        if observer_lock_owner(home, parent) is not None:
            raise ValueError('selected observer lock remains held after stop')
        report['phase'] = 'old_helpers_stopped'
        # Stopping a SQLite writer can leave a hot journal that read-only opens cannot recover.
        with closing(ledger_connection(home, 'rw')) as database:
            database.execute('SELECT name FROM sqlite_schema LIMIT 1').fetchone()
        report['journal_recovery_checked'] = True
        stopped = snapshot(home, parent)
        retained(before, stopped)
        save(evidence, 'stopped.json', stopped)
        with closing(ledger_connection(home)) as source, closing(sqlite3.connect(evidence / 'ledger.sqlite')) as destination:
            source.backup(destination)
        report['backup_sha256'] = digest(evidence / 'ledger.sqlite')
        root_idle(package, home, parent, generation)
        retained(stopped, snapshot(home, parent))
        environment = dict(os.environ, CODEX_HOME=str(home), PYTHONPATH=str(package),
                           PYTHONDONTWRITEBYTECODE='1')
        launched = time.time()
        with (evidence / 'new-supervisor.log').open('x') as output:
            child = subprocess.Popen([sys.executable, '-m', 'codex_plus.background', 'supervise',
                                      '--parent', parent], cwd=package, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                     start_new_session=True, close_fds=True)
        report.update(phase='new_supervisor_started', new_supervisor_pid=child.pid)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = helpers(parent, home)
            if any(row['cwd'] != str(package) for row in current):
                raise ValueError('foreign helper appeared after launch')
            after = snapshot(home, parent)
            retained(stopped, after)
            view = health(home, parent)
            fresh = bool(view and isinstance(view.get('updated_at'), (int, float)) and
                         view['updated_at'] >= launched)
            owner = observer_lock_owner(home, parent)
            if owner is not None and not any(row['action'] == 'observe' and row['pid'] == owner for row in current):
                observer = live_helper(owner, parent, home)
                if observer is None:
                    continue
                if (observer['action'] != 'observe' or observer['ppid'] != child.pid or
                        observer['cwd'] != str(package) or
                        observer['executable'] != str(Path(sys.executable).resolve())):
                    raise ValueError('selected observer lock acquired by an unrecognized owner after launch')
                current.append(observer)
            live = (len(current) == 2 and
                    any(row['action'] == 'supervise' and row['pid'] == child.pid for row in current) and
                    any(row['action'] == 'observe' and row['ppid'] == child.pid and row['pid'] == owner
                        for row in current))
            complete = child.poll() == 0 and not current
            if fresh and (live or complete):
                if view['state'] == 'error':
                    raise ValueError('new observer reports an error; inspect private evidence')
                save(evidence, 'after.json', after)
                report.update(phase='verified', new_helpers=current, health=view,
                              clean_complete_exit=complete, new_observer_lock_owner=owner)
                return report
            if child.poll() is not None and not complete:
                raise ValueError(f'new supervisor exited without verified completion: {child.returncode}')
            time.sleep(.1)
        raise ValueError('new observer did not establish a fresh verified heartbeat within 15 seconds')
    except Exception as exc:
        report.update(error=str(exc), new_supervisor_exit=child.poll() if child else None)
        try:
            save(evidence, 'partial.json', snapshot(home, parent))
        except (OSError, sqlite3.Error, ValueError) as partial_error:
            report['partial_read_error'] = type(partial_error).__name__
        raise
    finally:
        save(evidence, 'result.json', report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--inspect', action='store_true', help='default; read-only, no helper launch')
    mode.add_argument('--handoff', action='store_true')
    parser.add_argument('--parent', required=True, type=lambda value: str(uuid.UUID(value)))
    parser.add_argument('--codex-home', required=True, type=lambda value: Path(value).resolve(strict=True))
    parser.add_argument('--package', required=True, type=lambda value: Path(value).resolve(strict=True))
    parser.add_argument('--evidence-dir', type=lambda value: Path(value).absolute())
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        package = package_identity(args.package)
        before = snapshot(args.codex_home, args.parent)
        selected = helpers(args.parent, args.codex_home)
        result = {'mode': 'inspect', 'parent': args.parent, 'codex_home': str(args.codex_home),
                  'package': package, 'helpers': selected, 'health': health(args.codex_home, args.parent),
                  'needs_handoff': pending(before, args.parent),
                  'ledger_rows': {table: len(rows) for table, rows in before.items()}}
        if args.handoff and (result['needs_handoff'] or selected):
            if args.evidence_dir is None:
                raise ValueError('--handoff requires a new private --evidence-dir')
            result = handoff(args, before, selected)
        elif args.handoff:
            result['outcome'] = 'no handoff needed; no pending selected-parent ledger work or helpers'
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f'handoff refused or incomplete: {exc}; no ledger rollback or callback resend performed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
