"""Audit local supervised release work; never signal unrelated host processes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import secrets
import time


def process_identity(proc):
    return dict(start_ticks=(proc / 'stat').read_text().rsplit(')', 1)[1].split()[19],
                cmdline_sha256=hashlib.sha256((proc / 'cmdline').read_bytes()).hexdigest())


def control_ancestors(proc_root):
    ancestors = {}
    pid = os.getppid()
    while pid > 0 and str(pid) not in ancestors:
        try:
            fields = (proc_root / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        except (OSError, IndexError):
            break
        ancestors[str(pid)] = fields[19]
        pid = int(fields[1])
    return ancestors


def _identity(row):
    value = row.get('identity', row)
    return value['boot_id'], int(value['pid']), str(value['start_ticks'])


def verify_guard_journal(records, current_boot):
    """A terminal receipt is evidence only for its exact acknowledged supervisor."""
    if not records:
        return False
    starts, ends = {}, set()
    try:
        for row in records:
            identity = _identity(row)
            if identity[0] != current_boot or identity[1] <= 0 or not identity[2].isdigit():
                return False
            if not isinstance(row.get('argv'), list) or not row['argv']:
                return False
            if row.get('event') == 'started':
                if identity in starts or row.get('exit_code') is not None:
                    return False
                starts[identity] = row
            elif row.get('event') == 'finished':
                if identity not in starts or identity in ends or row['argv'] != starts[identity]['argv']:
                    return False
                if row.get('cmdline_sha256') != starts[identity].get('cmdline_sha256'):
                    return False
                # Nonzero can mean supervisor cleanup failed, not just its child.
                if type(row.get('exit_code')) is not int or row['exit_code'] != 0:
                    return False
                ends.add(identity)
            else:
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return bool(starts) and set(starts) == ends


def _under(path, roots):
    return any(path == root or root in path.parents for root in roots)


def inspect_processes(roots, baseline, *, scope, proc_root=Path('/proc'), current_boot=None,
                      recorded=(), completed_guards=False):
    boot = current_boot or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if baseline.get('boot_id') != boot:
        raise ValueError('Process evidence belongs to another boot')
    roots = [Path(root) for root in roots]
    scope = Path(scope)
    controls = control_ancestors(proc_root)
    event_journal = any('event' in row for row in recorded)
    terminal = verify_guard_journal(recorded, boot)
    if completed_guards and not terminal:
        raise ValueError('Completed supervision needs an exact terminal journal')
    owners = {_identity(row) for row in recorded}
    found, unknown, unchanged, unattributed = [], [], [], []
    if event_journal and not terminal:
        unknown.append(dict(reason='incomplete_guard_journal'))
    for proc in proc_root.glob('[0-9]*'):
        if proc.name == str(os.getpid()):
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                continue
            start = fields[19]
            owned = (boot, int(proc.name), start) in owners
            unreadable = False
            try:
                command_raw = (proc / 'cmdline').read_bytes()
                command = command_raw.split(b'\0')
                identity = dict(start_ticks=start, cmdline_sha256=hashlib.sha256(command_raw).hexdigest())
                if controls.get(proc.name) != start:
                    owned |= any(arg == os.fsencode(root) or arg.startswith(os.fsencode(root) + b'/')
                                 for root in roots for arg in command)
            except PermissionError:
                unreadable = True
                identity = dict(start_ticks=start, identity_unreadable=True)
            row = dict(pid=int(proc.name), **identity)
            try:
                env = (proc / 'environ').read_bytes().split(b'\0')
                owned |= f'CODEX_PLUS_RELEASE_QA_SCOPE={scope}'.encode() in env
                owned |= f'CODEX_PLUS_RELEASE_ATTEMPT={scope.name}'.encode() in env
                owned |= any(arg.startswith(b'CODEX_HOME=') and
                             _under(Path(os.fsdecode(arg[11:])), roots) for arg in env)
            except PermissionError:
                unreadable = True
            try:
                try:
                    cwd = (proc / 'cwd').resolve(strict=True)
                except FileNotFoundError:
                    cwd = Path(os.readlink(proc / 'cwd').removesuffix(' (deleted)'))
                owned |= _under(cwd, roots)
            except PermissionError:
                unreadable = True
            if owned:
                found.append(row)
            elif unreadable:
                if baseline.get('processes', {}).get(proc.name) == identity:
                    unchanged.append(row)
                elif completed_guards:
                    # The qualified local subreaper has reaped its tree. No claim
                    # about unrelated host processes, and no blanket SSH exemption.
                    unattributed.append(row)
                else:
                    unknown.append(row)
        except (FileNotFoundError, ProcessLookupError):
            # /proc may retain the directory after an exit removes its environment/cwd.
            try:
                terminal = (proc/'stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z'
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                unknown.append(dict(pid=int(proc.name), identity_unreadable=True))
                continue
            if not terminal:
                unknown.append(dict(pid=int(proc.name), reason='incomplete_process_snapshot'))
        except PermissionError:
            unknown.append(dict(pid=int(proc.name), identity_unreadable=True))
    return dict(passed=not found and not unknown, live_owned_processes=found,
                unknown_unreadable_same_user_processes=unknown,
                unchanged_preexisting_unreadable=unchanged,
                unattributed_host_processes=unattributed,
                completed_local_supervision=bool(completed_guards))


def capture_baseline(roots, scope):
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    result = inspect_processes(roots, dict(boot_id=boot, processes={}), scope=scope)
    rows = result['unknown_unreadable_same_user_processes']
    if result['live_owned_processes'] or any(row.get('identity_unreadable') or 'cmdline_sha256' not in row for row in rows):
        raise ValueError(f'Cannot establish pre-work ownership: {result}')
    return dict(boot_id=boot, recorded_at=time.time(), read_only=True,
                processes={str(row['pid']): {key: row[key] for key in ('start_ticks', 'cmdline_sha256')} for row in rows})


def validate_resolution(root, execution, metadata, plan, profile, state, result, resolution,
                        journal_sha256, baseline_sha256):
    import release_protocol as protocol
    expected = dict(attempt=metadata['attempt'], attempt_root=str(root),
                    plan_sha256=protocol.digest(plan), journal_sha256=journal_sha256,
                    ownership_baseline_sha256=baseline_sha256,
                    socket_identity=metadata['socket_identity'])
    proof = resolution.get('release_recovery', {})
    result_identity = state if result is None else result
    protocol.require(state.get('resolution') == resolution and
                     state.get('state') in ('failed', 'cancelled', 'unknown', 'completed') and
                     state.get('id') == result_identity.get('id') == resolution.get('id') == execution.name and
                     state.get('root') == result_identity.get('root') == resolution.get('root') and
                     bool(state.get('root')) and resolution.get('cleanup_verified') is True and
                     isinstance(resolution.get('evidence'), str) and bool(resolution['evidence'].strip()),
                     'Explicit matching managed cleanup resolution required')
    protocol.require(all(proof.get(key) == value for key, value in expected.items()) and
                     isinstance(proof.get('subtree_cleanup_evidence'), str) and
                     bool(proof['subtree_cleanup_evidence'].strip()), 'Recovery evidence binding mismatch')
    helper = Path(profile['executor_package_root']) / 'bin/codex-plus-runtime'
    protocol.require(protocol.recovery_results_match(state.get('result'), result) and
                     Path(state['package']).resolve() == Path(profile['executor_package_root']).resolve() and
                     state['runtime']['sha256'] == protocol.file_record(helper)['sha256'],
                     'Recovery executor/result identity mismatch')
    argv = state['manifest']['command']['spec']['argv']
    protocol.require(argv == [profile['python']['path'], '-B',
                     str(Path(profile['tooling_root']) / 'scripts/release_native.py'),
                     'run', '--attempt', str(root), '--execution', str(execution)],
                     'Recovery managed command mismatch')
    return expected


def recover_evidence(root, execution, records):
    import release_native as native
    import release_protocol as protocol
    native.verify(root)
    metadata, plan, profile = protocol.read_attempt(root)
    execution = Path(execution).resolve(strict=True)
    state, resolution = [protocol.load(execution / name) for name in ('state.json', 'resolution.json')]
    result_path = execution / 'result.json'
    result = protocol.load(result_path) if result_path.exists() else None
    protocol.require(result is not None or (state.get('state') == 'unknown' and state.get('result') is None),
                     'Missing managed result outside unresolved execution')
    journal_hash = protocol.file_record(root / 'process-journal.jsonl')['sha256']
    baseline_hash = protocol.file_record(root / 'ownership-baseline.json')['sha256']
    binding = validate_resolution(root, execution, metadata, plan, profile, state, result,
                                  resolution, journal_hash, baseline_hash)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    identities = []
    for row in records:
        protocol.require(row.get('event') in ('started', 'finished') and
                         isinstance(row.get('argv'), list) and bool(row['argv']), 'Malformed guard journal')
        identities.append(_identity(row))
    identities += [_identity(row) for row in [state.get('executor'), state.get('workload'),
                   *state.get('processes', [])] if row]
    absent = []
    for identity in sorted(set(identities)):
        old_boot, pid, ticks = identity
        protocol.require(old_boot == boot and pid > 0 and ticks.isdigit(), 'Recovery process binding mismatch')
        path = Path('/proc') / str(pid) / 'stat'
        try:
            current = path.read_text().rsplit(')', 1)[1].split()
        except FileNotFoundError:
            current = None
        protocol.require(current is None or current[19] != ticks or current[0] == 'Z',
                         'Recorded recovery process is still alive')
        absent.append(dict(boot_id=old_boot, pid=pid, start_ticks=ticks))
    # The explicit root resolution covers the missing journal end, not host PIDs.
    baseline = protocol.load(root / 'ownership-baseline.json')
    audit = inspect_processes([root, Path(metadata['socket_root'])], baseline, scope=root,
                              recorded=[dict(identity=row.get('identity', row)) for row in records])
    protocol.require(audit['passed'], f'Recovery ownership remains unresolved: {audit}')
    native.verify(root)
    protocol.require(protocol.file_record(root / 'process-journal.jsonl')['sha256'] == journal_hash and
                     protocol.file_record(root / 'ownership-baseline.json')['sha256'] == baseline_hash,
                     'Recovery inputs changed during verification')
    files = {}
    for name, value in [('state', state), ('result', result), ('resolution', resolution)]:
        path = root / 'recovery' / ('managed-' + name + '.json')
        if path.exists():
            protocol.require(protocol.load(path) == value, 'Preserve earlier recovery snapshot')
        else:
            protocol.write_once(path, value)
        files[path.relative_to(root).as_posix()] = protocol.file_record(path)
    for path in [root/'process-journal.jsonl', root/'ownership-baseline.json',
                 root/'native-preparation.json', *sorted((root/'native-starts').glob('*.json'))]:
        files[path.relative_to(root).as_posix()] = protocol.file_record(path)
    receipt = dict(binding=binding, files=files, absent=absent, audit=audit, result_absent=result is None,
                   seals_verified=True, qualification_unchanged=True)
    path = root / 'recovery/receipt.json'
    if path.exists():
        previous = protocol.load(path)
        protocol.require(all(previous.get(key) == receipt[key] for key in
                         ('binding', 'files', 'absent', 'result_absent', 'seals_verified', 'qualification_unchanged')),
                         'Recovery receipt binding changed; preserve earlier evidence')
    else:
        protocol.write_once(path, receipt)
    return path


def _remove_socket(root, socket, expected, protocol):
    receipt = root/'socket-removal.json'
    intent_path = root/'socket-removal-intent.json'
    quarantine_identity_path = root/'socket-quarantine.json'
    retries = sorted((root/'socket-cleanup-retries').glob('*-intent.json'))
    if retries:
        intent_path = retries[-1]
        quarantine_identity_path = intent_path.with_name(intent_path.name.replace('-intent','-quarantine'))
    if receipt.exists():
        protocol.require(protocol.load(receipt) == dict(identity=expected, removed=True),
                         'Socket removal receipt changed')
        protocol.require(not os.path.lexists(socket), 'Socket directory identity changed; preserve replacement')
        return
    if not intent_path.exists():
        protocol.require(os.path.lexists(socket), 'Missing socket without verified cleanup receipt')
        quarantine = socket.parent/('.release-cleanup-'+secrets.token_hex(12))
        protocol.write_once(intent_path, dict(identity=expected, quarantine=str(quarantine)))
    intent = protocol.load(intent_path)
    quarantine = Path(intent['quarantine'])
    protocol.require(intent['identity'] == expected and quarantine.parent == socket.parent and
                     quarantine.name.startswith('.release-cleanup-'), 'Cleanup intent changed')
    if not quarantine_identity_path.exists() and os.path.lexists(quarantine):
        # Creation was interrupted before identity was recorded; never adopt that directory.
        intent_path = root/'socket-cleanup-retries'/f'{len(retries)+1:06d}-intent.json'
        quarantine_identity_path = intent_path.with_name(intent_path.name.replace('-intent','-quarantine'))
        quarantine = socket.parent/('.release-cleanup-'+secrets.token_hex(12))
        protocol.write_once(intent_path, dict(identity=expected, quarantine=str(quarantine)))
    if not quarantine_identity_path.exists():
        quarantine.mkdir(mode=0o700)
        info = quarantine.lstat()
        protocol.write_once(quarantine_identity_path, dict(path=str(quarantine), dev=info.st_dev,
                            ino=info.st_ino, uid=info.st_uid))
    pinned = protocol.load(quarantine_identity_path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    qfd = os.open(quarantine, flags)
    try:
        info = os.fstat(qfd)
        protocol.require(pinned == dict(path=str(quarantine), dev=info.st_dev, ino=info.st_ino, uid=info.st_uid),
                         'Private cleanup directory identity changed')
        try:
            moved = os.stat('owned', dir_fd=qfd, follow_symlinks=False)
        except FileNotFoundError:
            # The verified moved inode may already have been deleted before an interruption.
            if (root/'socket-moved.json').exists():
                protocol.require(protocol.load(root/'socket-moved.json') == expected, 'Moved receipt changed')
                moved = None
            else:
                os.rename(socket, 'owned', dst_dir_fd=qfd)
                moved = os.stat('owned', dir_fd=qfd, follow_symlinks=False)
        if moved is not None:
            protocol.require(stat.S_ISDIR(moved.st_mode) and
                             (moved.st_dev,moved.st_ino,moved.st_uid) ==
                             (expected['dev'],expected['ino'],expected['uid']),
                             'Socket directory identity changed; preserved in quarantine')
            protocol.require(shutil.rmtree.avoids_symlink_attacks, 'Platform lacks safe directory cleanup')
            moved_receipt = root/'socket-moved.json'
            if moved_receipt.exists():
                protocol.require(protocol.load(moved_receipt) == expected, 'Moved receipt changed')
            else:
                protocol.write_once(moved_receipt, expected)
            shutil.rmtree('owned', dir_fd=qfd)
            os.fsync(qfd)
        if not (root/'socket-deleted.json').exists():
            protocol.write_once(root/'socket-deleted.json', expected)
        protocol.require(not os.path.lexists(socket), 'Socket path was replaced; preserve replacement')
        protocol.write_once(receipt, dict(identity=expected, removed=True))
    finally:
        os.close(qfd)
    # Keep the empty, pinned quarantine for forensic evidence; no second path deletion.


def audit_attempt(attempt, *, remove_socket=False, recovery_execution=None):
    import release_protocol as protocol
    root = Path(attempt).resolve(strict=True)
    protocol.require_native_attempt(root)
    metadata, _, profile = protocol.read_attempt(root)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    journal = root / 'process-journal.jsonl'
    records = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    baseline_path = root / 'ownership-baseline.json'
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else dict(boot_id=boot, processes={})
    socket = Path(metadata['socket_root'])
    if socket.parent.resolve() != Path(profile['short_root']).resolve() or not socket.name.isalnum():
        raise ValueError('Socket path is outside the declared namespace')
    recovery = recover_evidence(root, recovery_execution, records) if recovery_execution else None
    audited = [dict(identity=row.get('identity', row)) for row in records] if recovery else records
    result = inspect_processes([root, socket], baseline, scope=root, recorded=audited,
                               completed_guards=False if recovery else verify_guard_journal(records, boot))
    if recovery:
        result.update(recovery_receipt=recovery.relative_to(root).as_posix(),
                      recovery_receipt_sha256=protocol.file_record(recovery)['sha256'])
    result.update(outcome='clean' if result['passed'] else 'blocked',
                  owned_remaining=result['live_owned_processes'],
                  unknown=result['unknown_unreadable_same_user_processes'],
                  signals_sent=0, socket_removed=False,
                  socket_identity=metadata['socket_identity'])
    if remove_socket and result['passed']:
        expected = metadata['socket_identity']
        if expected['boot_id'] != boot or expected['path'] != str(socket):
            raise ValueError('Socket ownership binding changed')
        _remove_socket(root, socket, expected, protocol)
        result['socket_removed'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('audit', 'cleanup', 'recover'))
    parser.add_argument('--attempt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execution', type=Path)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(args.attempt.resolve()):
        raise ValueError('Cleanup report must remain inside its attempt')
    if (args.command == 'recover') != bool(args.execution):
        raise ValueError('--execution is required only for explicit recover')
    result = audit_attempt(args.attempt, remove_socket=args.command != 'audit',
                           recovery_execution=args.execution)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    import release_protocol as protocol
    protocol.write_once(args.output, result)
    if result['socket_removed']:
        report = args.output.resolve().relative_to(args.attempt.resolve()).as_posix()
        protocol.seal_socket_cleanup(args.attempt, report)
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
