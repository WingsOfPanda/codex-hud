"""Job-watch consumer acceptance in a private home, tmux server and native PTY.

Use the native-smoke Python interpreter. --collection-only verifies the private
fixture and real package without launching a TUI; it does not qualify the host.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

from fixtures.native_server import AppServer
from native_smoke import Terminal, composer_ready, wait
from test_mod_conformance import REPO, conformance
from test_ultracode_tui import helper_script


THREAD = '00000000-0000-0000-0065-000000000011'


def snapshot(directory):
    return {str(path.relative_to(directory)): dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                                  mtime_ns=path.stat().st_mtime_ns)
            for path in directory.rglob('*') if path.is_file()}


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return fields[0], fields[19]


def run(args):
    binary, node, source = [path.resolve(strict=True) for path in (args.binary, args.node, args.package_dir)]
    manifest, _ = conformance.package_manifest(source)
    assert manifest['id'] == 'job-watch', 'this acceptance fixture requires the real job-watch consumer'
    args.evidence.mkdir(parents=True, exist_ok=False)
    report = dict(passed=False, collection_only=args.collection_only, native_acceptance=False,
                  stock_daemon_compatibility=False, binary=str(binary),
                  binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), package=str(source),
                  source_receipt=json.loads((source / 'SOURCE.json').read_text()), stages=[])
    deadline = time.monotonic() + 160
    terminal = server = server_pid = server_identity = work_pid = work_identity = None
    with tempfile.TemporaryDirectory(prefix='plus-jw-') as temporary:
        root = Path(temporary)
        home, codex_home, work, package, mods = [root / name for name in ('home', 'codex', 'work', 'bin', 'mods')]
        for directory in (home, codex_home, work, package, mods):
            directory.mkdir(mode=0o700)
        records = home / '.claude/job-records'
        records.mkdir(parents=True)
        (home / '.claude/delivery-history.json').write_text('{"fixture":"must remain unchanged"}\n')
        shared_store = codex_home / 'codex-plus/job-store'
        shared_store.mkdir(parents=True)
        (shared_store / 'sentinel').write_bytes(b'private store: do not modify\n')
        installed = mods / manifest['id'] / manifest['version']
        shutil.copytree(source, installed)
        (installed.parent / 'current').symlink_to(installed.name)
        source_before, installed_before = snapshot(source), snapshot(installed)
        store_before = snapshot(shared_store)
        socket = root / 'tmux.sock'
        fixture_env = dict(HOME=str(home), PATH='/usr/bin:/bin', LANG='C.UTF-8')

        def tmux(*arguments, check=True):
            return subprocess.run(['/usr/bin/tmux', '-S', str(socket), '-f', '/dev/null', *arguments],
                                  env=fixture_env, cwd=work, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=5, check=check).stdout.strip()

        def remaining(limit=22):
            value = min(limit, deadline - time.monotonic())
            assert value > 0, 'consumer fixture exceeded its 160-second deadline'
            return value

        sentinel = root / 'record-command-executed'
        marker, logfile, gate = [work / name for name in ('work.rc', 'work.log', 'release')]
        started = datetime.fromtimestamp(time.time() - 5, timezone.utc).isoformat()
        owner = 'jw-owner'

        def record(title, *, record_owner=owner, pane='', **fields):
            values = dict(status='running', owner=record_owner, pane=pane, title=title, started=started,
                          progress=f'touch {sentinel}', show=f'touch {sentinel}', until=f'touch {sentinel}',
                          **fields)
            return ''.join(f'- {key}: {value}\n' for key, value in values.items())

        def direct_view():
            request_id = str(uuid.uuid4())
            request = dict(host_contract=1, op='view', request_id=request_id,
                           session=dict(thread_id=THREAD, codex_home=str(codex_home), tmux_pane=pane),
                           width=120, rows=64, now=int(time.time() * 1000))
            stdout, stderr = conformance.run_bounded([str(node), str(installed / manifest['entry'])],
                json.dumps(request).encode(), work, context, 5000)
            if stderr:
                report.setdefault('package_stderr', []).append(stderr.decode('utf-8', errors='replace'))
            reply = conformance.validate_reply(stdout, request_id)
            assert reply['status'] == 'ok', reply
            assert len(reply['rows']) == 2 and reply.get('overflow', 0) == 0, reply
            assert not any('FORBIDDEN-' in row['text'] for row in reply['rows']), reply
            return reply

        def observe(name, title, words, before):
            if terminal:
                wait([terminal], lambda: any(title in line and words in line for line in terminal.screen.display),
                     f'job-watch native row missing: {title} / {words}', remaining())
                assert 'FORBIDDEN-' not in terminal.text, terminal.text
                (args.evidence / f'{name}.txt').write_text(terminal.text + '\n')
                (args.evidence / f'{name}.ansi').write_bytes(terminal.output)
            reply = direct_view()
            assert any(title in row['text'] and words in row['text'] for row in reply['rows']), reply
            assert snapshot(home / '.claude') == before, 'refresh changed records or delivery history'
            assert snapshot(shared_store) == store_before, 'refresh changed private shared-store fixture'
            assert not sentinel.exists(), 'a record command ran'
            (args.evidence / f'{name}-reply.json').write_text(json.dumps(reply, indent=2) + '\n')
            report['stages'].append(name)

        try:
            pane = tmux('new-session', '-d', '-s', owner, '-P', '-F', '#{pane_id}', 'sleep 175')
            other_pane = tmux('split-window', '-d', '-t', owner, '-P', '-F', '#{pane_id}', 'sleep 175')
            other_owner_pane = tmux('new-session', '-d', '-s', 'jw-other', '-P', '-F', '#{pane_id}', 'sleep 175')
            server_pid = tmux('display-message', '-p', '-t', pane, '#{pid}')
            server_identity = process_identity(server_pid)
            session_id = tmux('display-message', '-p', '-t', pane, '#{session_id}').removeprefix('$')
            tmux_identity = f'{socket},{server_pid},{session_id}'
            context = dict(fixture_env, CODEX_HOME=str(codex_home), LC_ALL='C.UTF-8',
                           TMUX=tmux_identity, TMUX_PANE=pane)
            report['private_context'] = dict(home=str(home), codex_home=str(codex_home),
                                            tmux_socket=str(socket), tmux_pid=server_pid,
                                            tmux_identity=server_identity, owner=owner, pane=pane)
            (records / 'other-owner.md').write_text(record('FORBIDDEN-OWNER', record_owner='jw-other',
                pane=other_owner_pane, marker=str(work / 'other.rc')))
            (records / 'other-pane.md').write_text(record('FORBIDDEN-PANE', pane=other_pane,
                marker=str(work / 'other.rc')))
            workload = work / 'tiny-job.py'
            workload.write_text('from pathlib import Path\nimport time\n'
                f'log, marker, gate = map(Path, {list(map(str, (logfile, marker, gate)))!r})\n'
                'log.write_text("fixture started\\n")\nend = time.monotonic() + 145\n'
                'while not gate.exists() and time.monotonic() < end: time.sleep(.05)\n'
                'with log.open("a") as output: output.write("fixture finished\\n")\n'
                'marker.write_text("0\\n" if gate.exists() else "124\\n")\n')
            work_pid = tmux('new-session', '-d', '-s', 'jw-work', '-P', '-F', '#{pane_pid}',
                            f'exec /usr/bin/python3 {shlex.quote(str(workload))}')
            work_identity = process_identity(work_pid)
            report['private_context'].update(workload_pid=work_pid, workload_identity=work_identity)
            primary = records / 'primary.md'
            primary.write_text(record('consumer-local', pane=pane, marker=str(marker), log=str(logfile),
                                      job='jw-work', stall_after='300s'))
            if not args.collection_only:
                native = package / 'codex'
                shutil.copy2(binary, native)
                code_host = binary.with_name('codex-code-mode-host')
                if code_host.exists():
                    (package / code_host.name).symlink_to(code_host)
                helper = package / 'codex-plus-runtime'
                helper.write_text(helper_script((REPO / 'docs/ultracode-bootstrap.txt').read_text()))
                helper.chmod(0o700)
                launcher = root / 'private-codex'
                launcher.write_text('#!/bin/sh\n' + '\n'.join(
                    f'export {key}={shlex.quote(value)}' for key, value in context.items()) +
                    f'\nexec {shlex.quote(str(native))} "$@"\n')
                launcher.chmod(0o700)
                (codex_home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="openai"\n'
                    'check_for_update_on_startup=false\nsuppress_unstable_features_warning=true\n'
                    'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
                    '[tui]\nstatus_line=["thread-id"]\nscreen_reader_detection_done=true\n')
                (codex_home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
                (codex_home / 'codex-plus/mods.json').write_text(json.dumps(dict(
                    schema=1, root=str(mods), node=str(node), enabled=['job-watch'])))
                app_socket = codex_home / 'app-server-control/app-server-control.sock'
                app_socket.parent.mkdir()
                server = AppServer(THREAD, work, 0, socket_path=app_socket)
                terminal = Terminal(launcher, codex_home, work)
                wait([terminal], lambda: composer_ready(terminal.text), 'private composer did not start', remaining(25))
            before = snapshot(home / '.claude')
            observe('running', 'consumer-local', 'running', before)
            gate.touch()
            end = time.monotonic() + remaining(5)
            while not marker.exists() and time.monotonic() < end:
                if terminal:
                    terminal.read()
                else:
                    time.sleep(.02)
            assert marker.read_text() == '0\n', 'controlled tmux job did not finish successfully'
            observe('done', 'consumer-local', 'finished', before)
            for name, title, words, fields in (
                ('failed', 'consumer-failed', 'failed (exit 7)', dict(marker=str(work / 'failed.rc'))),
                ('stalled', 'consumer-stalled', 'log quiet, may be healthy',
                 dict(marker=str(work / 'absent.rc'), log=str(work / 'stale.log'), stall_after='1s')),
                ('unavailable', 'consumer-unavailable', "can't read: until records are not read by this host",
                 dict(kind='until')),
            ):
                (work / 'failed.rc').write_text('7\n')
                stale = work / 'stale.log'
                stale.write_text('old fixture output\n')
                os.utime(stale, (time.time() - 120, time.time() - 120))
                primary.write_text(record(title, pane=pane, **fields))
                observe(name, title, words, snapshot(home / '.claude'))
            if terminal:
                os.write(terminal.master, b'consumer-editable-draft')
                wait([terminal], lambda: 'consumer-editable-draft' in terminal.text,
                     'consumer rows blocked composer typing', remaining(5))
                (args.evidence / 'editable.txt').write_text(terminal.text + '\n')
                report['native_acceptance'] = True
            assert snapshot(source) == source_before and snapshot(installed) == installed_before, 'package changed'
            report.update(passed=True, records_unchanged_by_refresh=True, package_unchanged=True,
                          record_commands_executed=0, shared_store_unchanged=True)
        except BaseException as error:
            report['failure'] = str(error)
            raise
        finally:
            if terminal:
                (args.evidence / 'last-screen.txt').write_text(terminal.text + '\n')
                (args.evidence / 'terminal.ansi').write_bytes(terminal.output)
                terminal.close()
            requests = server.requests if server else []
            forbidden = {'turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start'}
            report['display_model_requests'] = sum(request.get('method') in forbidden for request in requests)
            report['server_errors'] = server.errors if server else []
            if server:
                server.close()
            (args.evidence / 'rpc.json').write_text(json.dumps(requests, indent=2) + '\n')
            (args.evidence / 'records.json').write_text(json.dumps(snapshot(home / '.claude'), indent=2) + '\n')
            if socket.exists():
                tmux('kill-server', check=False)
            cleanup_end = time.monotonic() + 2
            identity = process_identity(server_pid) if server_pid else None
            while (identity and identity[0] != 'Z' and identity[1] == server_identity[1]
                   and time.monotonic() < cleanup_end):
                time.sleep(.02)
                identity = process_identity(server_pid)
            report['private_tmux_no_live_server'] = (identity is None or identity[0] == 'Z'
                                                    or identity[1] != server_identity[1])
            if report['private_tmux_no_live_server']:
                socket.unlink(missing_ok=True)
            report['private_tmux_removed'] = not socket.exists()
            identity = process_identity(work_pid) if work_pid else None
            report['private_workload_no_live_process'] = (identity is None or identity[0] == 'Z'
                                                          or identity[1] != work_identity[1])
            report['passed'] &= (not report['display_model_requests'] and not report['server_errors']
                                 and report['private_tmux_removed'] and report['private_workload_no_live_process'])
            (args.evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    assert report['passed'], report
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--node', required=True, type=Path)
    parser.add_argument('--package-dir', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--collection-only', action='store_true')
    run(parser.parse_args())
