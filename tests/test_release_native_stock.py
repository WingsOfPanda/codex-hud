"""Private stock-daemon release failure/recovery qualification.

--self-test never starts a daemon or job and never sends a signal. Actual cases
require reviewed plan/profile data and a new evidence directory. All native CLI
mutations use the fixture's official native-tool admission and private CODEX_HOME.
"""
import argparse
import copy
import ctypes
import json
import os
from pathlib import Path
import platform
import select
import shutil
import signal
import subprocess
import sys
import time
import traceback
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT)]
import release_native as native
import release_ownership as ownership
import release_protocol as protocol
from fixtures.detached_jobs import DetachedJobServer
from test_background_integration import stop, wait
from test_rust_runtime_stock_integration import stop_rust_observers


class Blocked(RuntimeError):
    pass


def identity_key(row):
    return row['boot_id'], int(row['pid']), int(row['start_ticks'])


def process(pid, proc=Path('/proc')):
    fields = (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'boot_id': (proc / 'sys/kernel/random/boot_id').read_text().strip(),
            'pid': int(pid), 'start_ticks': int(fields[19]), 'parent': int(fields[1]),
            'state': fields[0]}


def pin(identity):
    descriptor = os.pidfd_open(int(identity['pid']))
    try:
        if identity_key(process(identity['pid'])) != identity_key(identity):
            raise Blocked('Process boot/start identity changed; do not signal or reap')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def send_exact(identity, sig):
    descriptor = pin(identity)
    try:
        signal.pidfd_send_signal(descriptor, sig)
    finally:
        os.close(descriptor)


def child_ids(pid, proc=Path('/proc')):
    result = set()
    for task in (proc / str(pid) / 'task').iterdir():
        result.update(int(value) for value in (task / 'children').read_text().split())
    return result


def descendants(pid, proc=Path('/proc')):
    result, pending = {}, [pid]
    while pending:
        parent = pending.pop()
        try:
            children = child_ids(parent, proc)
        except FileNotFoundError:
            continue
        for child in children:
            try:
                row = process(child, proc)
            except FileNotFoundError:
                continue
            if row['parent'] != parent:
                continue
            result[identity_key(row)] = row
            pending.append(child)
    return result


class KernelBoundary:
    """A dedicated test-process boundary; the production owned guard is unchanged."""
    def __init__(self):
        if platform.system() != 'Linux' or not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise Blocked('Linux pidfd and child-subreaper support are required before fixture spawn')
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            prctl = libc.prctl
        except (AttributeError, OSError) as error:
            raise Blocked('Linux child-subreaper control is unavailable') from error
        if prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
            raise Blocked('Cannot establish private test subreaper')
        flag = ctypes.c_int()
        if prctl(37, ctypes.byref(flag), 0, 0, 0) or flag.value != 1:
            raise Blocked('Private test subreaper flag was not confirmed')
        self.identity = process(os.getpid())
        self.reaped = []
        if descendants(os.getpid()):
            raise Blocked('This harness must start without existing children')

    def quiet(self, controls, *, timeout=20):
        """Never steal a Popen status; reap only exact non-control adopted children."""
        allowed = {identity_key(row) for row in controls}
        end, stable = time.monotonic() + timeout, 0
        while time.monotonic() < end:
            rows = descendants(os.getpid())
            unexpected = {key: row for key, row in rows.items() if key not in allowed}
            for row in list(unexpected.values()):
                if row['parent'] != os.getpid() or row['state'] != 'Z':
                    continue
                descriptor = pin(row)
                try:
                    if not select.select([descriptor], [], [], 0)[0]:
                        raise Blocked('Adopted child is not terminal')
                    waited, status = os.waitpid(row['pid'], os.WNOHANG)
                    if waited != row['pid']:
                        raise Blocked('Exact adopted child was not reaped')
                    self.reaped.append({'identity': row, 'wait_status': status,
                                        'exit_code': os.waitstatus_to_exitcode(status)})
                finally:
                    os.close(descriptor)
            if not unexpected:
                stable += 1
                if stable == 2:
                    return {'boundary': self.identity, 'subreaper_confirmed': True,
                            'retained_controls': list(rows.values()), 'reaped': self.reaped,
                            'noncontrol_descendants': [], 'stable_scans': stable}
            else:
                stable = 0
            time.sleep(.05)
        raise Blocked(f'Private kernel descendants remain: {list(unexpected.values())}')


class RetainedFixture(DetachedJobServer):
    verified_teardown = False

    def launch(self, **kwargs):
        # Explicit close still deletes the home; GC must never erase blocked state.
        self.temporary._finalizer.detach()
        return super().launch(**kwargs)

    def close(self, passed):
        if not self.verified_teardown:
            if hasattr(self, 'temporary'):
                self.temporary._finalizer.detach()
            return
        super().close(passed)


def preserve(fixture, destination):
    destination.mkdir(mode=0o700)
    for name, source in (('executions', fixture.home / 'codex-plus/executions'),
                         ('work', fixture.work), ('executor-package', fixture.runtime.parents[1])):
        if source.exists():
            shutil.copytree(source, destination / name)
    directory = fixture.home / 'codex-plus'
    if directory.exists():
        ledger = destination / 'private-ledger'
        ledger.mkdir()
        for path in directory.iterdir():
            if path.is_file() and path.suffix in ('.json', '.sqlite', '.sqlite-wal', '.sqlite-shm'):
                shutil.copy2(path, ledger / path.name)
    protocol.write_once(destination / 'retained-paths.json', {
        'home': str(fixture.home), 'work': str(fixture.work), 'runtime': str(fixture.runtime),
        'ledger_copy': 'forensic bytes; no consistent live SQLite snapshot is claimed'})


def stop_controls(fixture):
    """Retain private home and unresolved executions when teardown is blocked."""
    stop_rust_observers(fixture.home)
    if getattr(fixture, 'rpc', None):
        fixture.rpc.close()
        fixture.rpc = None
    if getattr(fixture, 'native', None):
        stop(fixture.native)
    if getattr(fixture, 'provider', None):
        fixture.provider.close()
    if getattr(fixture, 'supervisor_lock', None):
        fixture.supervisor_lock.close()
    for child, log in getattr(fixture, 'processes', []):
        stop(child)
        log.close()
    if getattr(fixture, 'log', None):
        fixture.log.close()


def operations(fixture, rows):
    if fixture.env.get('CODEX_HOME') != str(fixture.home) or not fixture.runtime.is_relative_to(fixture.root):
        raise Blocked('Native admission must remain inside the private fixture home/package')
    command = fixture.commands(rows)
    responses = fixture.responses(command)
    fixture.finish(command)
    if any(row['returncode'] != 0 for row in responses):
        raise Blocked(f'Private admitted native command failed: {responses}')
    return responses


def unmatched_guard(attempt):
    path = attempt / 'process-journal.jsonl'
    try:
        records = [json.loads(line) for line in path.read_text().splitlines()]
    except (FileNotFoundError, ValueError):
        return None
    ended = {identity_key(row['identity']) for row in records if row['event'] == 'finished'}
    return next((row for row in reversed(records)
                 if row['event'] == 'started' and identity_key(row['identity']) not in ended), None)


def interrupt(fixture, ident, attempt, evidence):
    start = wait(lambda: unmatched_guard(attempt), 'No actual unmatched owned-guard start', timeout=25)
    current = fixture.job(ident)
    workload = current.get('workload')
    if current['state'] != 'running' or not workload:
        raise Blocked('Managed workload ended before the interruption checkpoint')
    descriptor = pin(workload)
    stopped = False
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
        stopped = True
        wait(lambda: process(workload['pid'])['state'] == 'T', 'Workload did not stop', timeout=3)
        pinned_start = unmatched_guard(attempt)
        if not pinned_start or pinned_start['identity'] != start['identity']:
            raise Blocked('The unmatched guard checkpoint was missed; no timing inference')
        guard_fd = pin(start['identity'])
        try:
            if select.select([guard_fd], [], [], 0)[0]:
                raise Blocked('The checkpoint guard was already terminal')
            protocol.write_once(evidence / 'interruption-checkpoint.json', {
                'managed_workload': workload, 'guard_start': pinned_start,
                'journal_sha256': native.sha(attempt / 'process-journal.jsonl'),
                'kernel_descendants': list(descendants(workload['pid']).values()),
                'signal_target': 'exact pidfd-bound managed workload; guard/runtime are unchanged'})
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            stopped = False
            if not select.select([descriptor], [], [], 8)[0]:
                raise Blocked('Killed managed workload did not become terminal')
        finally:
            os.close(guard_fd)
    finally:
        if stopped:
            # Restore only this pinned process after a missed checkpoint; retain state.
            signal.pidfd_send_signal(descriptor, signal.SIGCONT)
        os.close(descriptor)


def original_records(attempt, execution):
    paths = [attempt / 'process-journal.jsonl', *sorted((attempt / 'receipts').glob('*.json'))]
    if (execution / 'result.json').exists():
        paths.append(execution / 'result.json')
    return {str(path): native.sha(path) for path in paths}


def independent_cleanup(fixture, ident, attempt, kernel, controls, evidence):
    current = wait(lambda: (row := fixture.job(ident)).get('result') is not None and row,
                   'No durable managed failure result', timeout=35)
    if current['state'] not in ('failed', 'cancelled') or current['result']['state'] != current['state']:
        raise Blocked('Expected real managed failure was not preserved')
    stop_rust_observers(fixture.home)
    # Kernel ancestry includes unmarked, env-clearing and orphaned private children.
    proof = kernel.quiet(controls)
    metadata, _, _ = protocol.read_attempt(attempt)
    journal = protocol.load(attempt / 'ownership-baseline.json')
    rows = [json.loads(line) for line in (attempt / 'process-journal.jsonl').read_text().splitlines()]
    audit = ownership.inspect_processes([attempt, Path(metadata['socket_root'])], journal,
        scope=attempt, recorded=[{'identity': row['identity']} for row in rows], completed_guards=False)
    if not audit['passed']:
        raise Blocked(f'Independent conservative ownership audit blocked: {audit}')
    proof.update(managed_state=current['state'], managed_result=current['result'],
                 private_root=fixture.parent, private_home=str(fixture.home), scope_audit=audit)
    protocol.write_once(evidence / 'subtree-cleanup-proof.json', proof)
    return current, proof


def recover(fixture, ident, attempt, proof, evidence):
    execution = fixture.job_directory(ident)
    metadata, plan, profile = protocol.read_attempt(attempt)
    resolution = {'root': fixture.parent, 'id': ident, 'cleanup_verified': True,
        'evidence': f'Private kernel-subreaper and conservative scope evidence at {evidence}',
        'release_recovery': {'attempt': metadata['attempt'], 'attempt_root': str(attempt),
            'plan_sha256': protocol.digest(plan), 'journal_sha256': native.sha(attempt / 'process-journal.jsonl'),
            'ownership_baseline_sha256': native.sha(attempt / 'ownership-baseline.json'),
            'socket_identity': metadata['socket_identity'],
            'subtree_cleanup_evidence': json.dumps(proof, sort_keys=True)}}
    path = evidence / 'resolution-input.json'
    protocol.write_once(path, resolution)
    operations(fixture, [['job', 'resolve', '--id', ident, '--evidence', str(path)]])
    output = attempt / 'outputs/recovery-final.json'
    argv = [profile['python']['path'], '-B', str(ROOT / 'scripts/release_ownership.py'),
            'recover', '--attempt', str(attempt), '--execution', str(execution), '--output', str(output)]
    env = native.python_env(profile) | {'CODEX_HOME': str(fixture.home)}
    with (evidence / 'ownership-recover.log').open('xb') as log:
        result = subprocess.run(argv, env=env, cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, timeout=90)
    if result.returncode or not output.exists() or protocol.load(output)['outcome'] != 'clean':
        raise Blocked('Ownership recovery remains blocked; preserve private home and original records')


def run_case(args):
    evidence = args.evidence.resolve()
    if evidence.exists():
        raise ValueError('Use a new evidence directory')
    source_runtime = args.runtime.resolve(strict=True)
    source_manifest = protocol.load(source_runtime.parents[1] / 'codex-plus.json')
    if source_manifest['release'] != '0.163.0-plus.0.4.7' or source_manifest['files']['bin/codex-plus-runtime']['sha256'] != native.sha(source_runtime):
        raise ValueError('Supply the reviewed 0.4.7 helper package')
    plan, profile = protocol.load(args.plan), protocol.load(args.profile)
    if source_manifest['target'] != profile['triple'] or profile['architecture'] != platform.machine():
        raise ValueError('Helper, profile and native host architectures must agree')
    if Path(profile['tooling_root']).resolve() != ROOT or profile['python']['path'] != str(Path(profile['python']['path']).resolve()):
        raise ValueError('Freeze clean tooling and use the canonical pinned interpreter')
    if native.sha(args.stock_binary.resolve(strict=True)) != profile['native_build']['official_sha256']:
        raise ValueError('Wrong official stock binary')
    protocol.validate_plan(plan, profile['source_root'])
    protocol.validate_profile(profile, profile['architecture'])
    if protocol.digest(profile) != plan['targets'][profile['architecture']]['profile_sha256']:
        raise ValueError('The reviewed plan/profile digest does not match')
    kernel = KernelBoundary()  # Must precede every private fixture spawn.
    evidence.mkdir(parents=True, mode=0o700)
    fixture = RetainedFixture.__new__(RetainedFixture)
    report = {'passed': False, 'blocked': True, 'scenario': args.scenario,
              'remote_model_requests': 0, 'live_root_ledger_commands': 0}
    ready_to_close, attempt = False, None
    try:
        fixture.__init__(source_runtime, args.stock_binary.resolve(strict=True), evidence / 'fixture',
                         config='features.apps=false\nfeatures.memories=false\n')
        if fixture.report['stock_version'] != 'codex-cli 0.163.0':
            raise Blocked('The private fixture requires official 0.163.0')
        if fixture.home.resolve() == Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve():
            raise Blocked('Fixture must not use a live CODEX_HOME')
        if protocol.file_record(fixture.runtime) != protocol.file_record(source_runtime):
            raise Blocked('Private copied executor differs from supplied package')
        shutil.copy2(source_runtime.parents[1] / 'codex-plus.json', fixture.runtime.parents[1] / 'codex-plus.json')
        operations(fixture, [['job', 'list']])
        controls = list(descendants(os.getpid()).values())
        protocol.write_once(evidence / 'kernel-control-baseline.json', controls)
        profile = copy.deepcopy(profile)
        profile.update(executor_package_root=str(fixture.runtime.parents[1]),
                       build_root=str(evidence / 'native-build'), short_root='/tmp')
        plan = copy.deepcopy(plan)
        plan['targets'][profile['architecture']]['profile_sha256'] = protocol.digest(profile)
        observed, _ = native.observe(profile)
        attempt = protocol.init_attempt(plan, profile, profile['source_root'], observed=observed, rehearsal=True)
        hook = {'argv': [profile['python']['path'], '-B', str(ROOT / 'scripts/release_ownership.py'),
                        'audit', '--attempt', '{attempt}', '--output', '{report}'],
                'cwd': str(ROOT), 'env': native.python_env(profile), 'deadline_seconds': 90}
        native.prepare(attempt, hook, rehearsal_failure='frontend' if args.scenario == 'injected-failure' else None)
        proposal = attempt / 'job-proposal.json'
        rows = operations(fixture, [
            ['job', 'prepare', '--manifest', str(proposal), '--turn', '{turn}'],
            ['job', 'launch', '--id', '{id}', '--review-digest', '{digest}',
             '--reason', 'Private non-publishable native failure qualification']])
        ident = rows[0]['output']['id']
        execution = fixture.job_directory(ident)
        report.update(attempt=str(attempt), private_home=str(fixture.home), execution=str(execution),
                      private_root=fixture.parent, runtime_sha256=native.sha(fixture.runtime))
        if args.scenario == 'workload-interruption':
            interrupt(fixture, ident, attempt, evidence)
        current, proof = independent_cleanup(fixture, ident, attempt, kernel, controls, evidence)
        journal = [json.loads(line) for line in (attempt / 'process-journal.jsonl').read_text().splitlines()]
        if args.scenario == 'injected-failure':
            if protocol.load(attempt / 'receipts/preflight.json')['input']['execution']['outcome'] != 'passed' or \
                    protocol.load(attempt / 'receipts/frontend.json')['input']['execution']['outcome'] != 'failed' or \
                    not any(row['event'] == 'finished' and row['exit_code'] != 0 for row in journal):
                raise Blocked('Injected frontend failure did not preserve its expected receipts')
        elif not unmatched_guard(attempt) or current['result']['signal'] != signal.SIGKILL:
            raise Blocked('Interruption did not preserve unmatched journal and actual signal result')
        if current['result']['cleanup']['status'] != 'failed':
            raise Blocked('Failed/incomplete guard was incorrectly treated as clean')
        original = original_records(attempt, execution)
        preserve(fixture, evidence / 'before-resolution')
        recover(fixture, ident, attempt, proof, evidence)
        if any(native.sha(path) != digest for path, digest in original.items()):
            raise Blocked('Recovery changed original failed receipts, result or guard journal')
        native.verify(attempt)
        bundle = evidence / 'failed-evidence.tar.gz'
        archive = protocol.export_attempt(attempt, bundle)
        imported = protocol.import_attempt(bundle, evidence / 'imported-evidence', expected_sha256=archive['sha256'])
        for path, digest in original.items():
            source = Path(path)
            if source.is_relative_to(attempt) and native.sha(imported / source.relative_to(attempt)) != digest:
                raise Blocked('Evidence import changed an original failure record')
        try:
            protocol.accept_attempt(imported, source_root=profile['source_root'])
        except ValueError as error:
            if not any(word in str(error).lower() for word in ('missing', 'failed', 'rehearsal', 'cleanup')):
                raise Blocked(f'Unexpected acceptance error: {error}') from error
            report['acceptance_refusal'] = str(error)
        else:
            raise Blocked('Failed rehearsal evidence was accepted')
        stop_rust_observers(fixture.home)
        report['final_kernel_proof'] = kernel.quiet(controls)
        preserve(fixture, evidence / 'before-close')
        final_state = protocol.load(execution / 'state.json')['state']
        if final_state != current['state']:
            raise Blocked('Explicit resolution changed the original managed failure outcome')
        report.update(passed=True, blocked=False, original_records=original,
                      managed_failure_state=final_state, bundle=archive,
                      fixture_home_will_be_removed=True, native_attempt_evidence_retained=True)
        ready_to_close = True
    except BaseException:
        report['error'] = traceback.format_exc()
        if hasattr(fixture, 'home'):
            fixture.temporary._finalizer.detach()
            report['retained_private_home'] = str(fixture.home)
            try:
                preserve(fixture, evidence / 'blocked-private-state')
            except BaseException:
                report['snapshot_error'] = traceback.format_exc()
    finally:
        if hasattr(fixture, 'home'):
            try:
                if ready_to_close:
                    fixture.verified_teardown = True
                    fixture.close(report['passed'])
                else:
                    stop_controls(fixture)
            except BaseException:
                report.update(passed=False, blocked=True, teardown_error=traceback.format_exc())
        protocol.write_once(evidence / 'report.json', report)
    return report


class SelfChecks(unittest.TestCase):
    def test_nonlinux_refuses_before_prctl_or_fixture_spawn(self):
        with patch.object(platform, 'system', return_value='Darwin'), patch.object(ctypes, 'CDLL') as libc:
            with self.assertRaises(Blocked):
                KernelBoundary()
            libc.assert_not_called()

    def test_failed_prctl_refuses_without_fixture_spawn(self):
        with patch.object(platform, 'system', return_value='Linux'), patch.object(ctypes, 'CDLL') as libc, \
                patch.object(os, 'pidfd_open', create=True), patch.object(signal, 'pidfd_send_signal', create=True):
            libc.return_value.prctl.return_value = -1
            with self.assertRaisesRegex(Blocked, 'Cannot establish'):
                KernelBoundary()

    def test_missing_pidfd_support_refuses_before_prctl(self):
        with patch.object(platform, 'system', return_value='Linux'), \
                patch.dict(globals(), os=object()), patch.object(ctypes, 'CDLL') as libc:
            with self.assertRaisesRegex(Blocked, 'support are required'):
                KernelBoundary()
            libc.assert_not_called()

    def test_unconfirmed_subreaper_flag_refuses(self):
        with patch.object(platform, 'system', return_value='Linux'), patch.object(ctypes, 'CDLL') as libc, \
                patch.object(os, 'pidfd_open', create=True), patch.object(signal, 'pidfd_send_signal', create=True):
            libc.return_value.prctl.return_value = 0
            with self.assertRaisesRegex(Blocked, 'not confirmed'):
                KernelBoundary()

    def test_changed_pid_identity_cannot_be_signalled(self):
        row = {'boot_id': 'boot', 'pid': 7, 'start_ticks': 12}
        with patch.object(os, 'pidfd_open', return_value=55, create=True), patch.object(os, 'close') as close, \
                patch.dict(globals(), process=Mock(return_value=dict(row, start_ticks=13))), \
                patch.object(signal, 'pidfd_send_signal', create=True) as send:
            with self.assertRaises(Blocked):
                send_exact(row, signal.SIGKILL)
            send.assert_not_called()
            close.assert_called_once_with(55)

    def test_unresolved_fixture_close_never_deletes_private_home(self):
        fixture = RetainedFixture.__new__(RetainedFixture)
        fixture.temporary = Mock()
        with patch.object(DetachedJobServer, 'close') as close:
            fixture.close(False)
            fixture.temporary._finalizer.detach.assert_called_once()
            close.assert_not_called()

    def test_known_popen_control_is_never_reaped(self):
        row = {'boot_id': 'boot', 'pid': 7, 'start_ticks': 12, 'parent': os.getpid(), 'state': 'Z'}
        kernel = KernelBoundary.__new__(KernelBoundary)
        kernel.identity, kernel.reaped = row, []
        with patch.dict(globals(), descendants=Mock(return_value={identity_key(row): row})), \
                patch.object(os, 'waitpid') as reap, patch.object(time, 'sleep'):
            proof = kernel.quiet([row])
            reap.assert_not_called()
            self.assertEqual(proof['noncontrol_descendants'], [])

    def test_exact_adopted_zombie_is_reaped_with_its_actual_status(self):
        row = {'boot_id': 'boot', 'pid': 7, 'start_ticks': 12, 'parent': os.getpid(), 'state': 'Z'}
        kernel = KernelBoundary.__new__(KernelBoundary)
        kernel.identity, kernel.reaped = row, []
        snapshots = [{identity_key(row): row}, {}, {}]
        with patch.dict(globals(), descendants=Mock(side_effect=snapshots), pin=Mock(return_value=55)), \
                patch.object(select, 'select', return_value=([55], [], [])), \
                patch.object(os, 'waitpid', return_value=(7, 7 << 8)) as reap, \
                patch.object(os, 'close'), patch.object(time, 'sleep'):
            proof = kernel.quiet([])
        reap.assert_called_once_with(7, os.WNOHANG)
        self.assertEqual(proof['reaped'][0]['exit_code'], 7)

    def test_unexpected_live_descendant_blocks_without_reaping(self):
        row = {'boot_id': 'boot', 'pid': 7, 'start_ticks': 12, 'parent': os.getpid(), 'state': 'S'}
        kernel = KernelBoundary.__new__(KernelBoundary)
        kernel.identity, kernel.reaped = row, []
        with patch.dict(globals(), descendants=Mock(return_value={identity_key(row): row})), \
                patch.object(time, 'monotonic', side_effect=[0, 0, 2]), \
                patch.object(time, 'sleep'), patch.object(os, 'waitpid') as reap:
            with self.assertRaisesRegex(Blocked, 'descendants remain'):
                kernel.quiet([], timeout=1)
            reap.assert_not_called()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--stock-binary', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--scenario', choices=('injected-failure', 'workload-interruption'))
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SelfChecks))
        return 0 if result.wasSuccessful() else 1
    if not all((args.runtime, args.stock_binary, args.plan, args.profile, args.evidence, args.scenario)):
        parser.error('Supply every private scenario input, or use --self-test')
    os.umask(0o077)
    def stopped(signum, frame):
        raise Blocked(f'Private harness interrupted by signal {signum}; retain unresolved state')
    signal.signal(signal.SIGTERM, stopped)
    report = run_case(args)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
