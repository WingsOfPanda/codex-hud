"""Real frontend, private PTYs and loopback-only app-server protocol fixtures.

Run with the existing native-smoke Python interpreter (pyte required). One binary
is copied once and reused while package current links advance and roll back.
This is frontend acceptance, not stock-daemon or package-domain qualification.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from fixtures.native_server import AppServer
from native_smoke import Terminal, composer_ready, wait
from test_mod_conformance import NODE, REPO, change_manifest, conformance, make_package
from test_ultracode_tui import helper_script


THREAD = '00000000-0000-0000-0065-000000000001'


def traces(home):
    path = home / 'mod-fixture-calls.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def point(root, identifier, version):
    current = root / identifier / 'current'
    temporary = current.with_name('next')
    temporary.symlink_to(version)
    temporary.replace(current)


def run(args):
    binary, node = args.binary.resolve(strict=True), args.node.resolve(strict=True)
    args.evidence.mkdir(parents=True, exist_ok=False)
    report = dict(passed=False, binary=str(binary), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                  fixture='private native PTY, private app-server protocol fixture, helper stub',
                  stock_daemon_compatibility=False, package_domain_acceptance=False, stages=[])
    terminals, servers, homes = [], [], []
    with tempfile.TemporaryDirectory(prefix='plus-mod-') as temporary:
        root = Path(temporary)
        package, work, mods = [root / name for name in ('bin', 'work', 'mods')]
        for directory in (package, work, mods):
            directory.mkdir(mode=0o700)
        native = package / 'codex'
        shutil.copy2(binary, native)
        code_host = binary.with_name('codex-code-mode-host')
        if code_host.exists():
            (package / code_host.name).symlink_to(code_host)
        helper = package / 'codex-plus-runtime'
        helper.write_text(helper_script((REPO / 'docs/ultracode-bootstrap.txt').read_text()))
        helper.chmod(0o700)
        make_package(mods, trace=True, rows=5)
        make_package(mods, version='2.0.0', trace=True, rows=5)
        make_package(mods, 'garden-panel', trace=True, rows=5, mode='stderr')
        unsupported = make_package(mods, 'unsupported-panel', trace=True)
        change_manifest(unsupported, provides=['unimplemented.rows'])
        make_package(mods, 'broken-panel', trace=True, mode='wrong-id')
        make_package(mods, 'disabled-panel', trace=True)
        enabled = ['orbit-panel', 'garden-panel', 'unsupported-panel', 'broken-panel']
        for identifier in enabled + ['disabled-panel']:
            point(mods, identifier, '1.0.0')
        # Optional consumer is copied into this fixture root, never advanced in place.
        for source in args.package_dir:
            source = source.resolve(strict=True)
            manifest, _ = conformance.package_manifest(source)
            destination = mods / manifest['id'] / manifest['version']
            shutil.copytree(source, destination, symlinks=True)
            point(mods, manifest['id'], manifest['version'])
            enabled.append(manifest['id'])
        payload_before = {str(path.relative_to(mods)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in mods.rglob('*') if path.is_file() and 'current' not in path.parts}

        def capture(terminal, name):
            (args.evidence / f'{name}.txt').write_text(terminal.text + '\n')
            (args.evidence / f'{name}.ansi').write_bytes(terminal.output)

        def settle(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                for terminal in terminals:
                    terminal.read()
                    assert terminal.process.poll() is None, terminal.text

        def start(name, *, zero=False):
            home = root / name
            home.mkdir(mode=0o700)
            homes.append(home)
            (home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="openai"\n'
                'check_for_update_on_startup=false\nsuppress_unstable_features_warning=true\n'
                'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
                '[tui]\nstatus_line=["thread-id"]\nscreen_reader_detection_done=true\n')
            (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
            config = home / 'codex-plus/mods.json'
            config.parent.mkdir()
            config.write_text(json.dumps(dict(schema=1, root=str(mods), node=str(node),
                                              enabled=[] if zero else enabled)))
            socket = home / 'app-server-control/app-server-control.sock'
            socket.parent.mkdir()
            server = AppServer(THREAD, work, 0, socket_path=socket)
            servers.append(server)
            terminal = Terminal(native, home, work)
            terminals.append(terminal)
            wait(terminals, lambda: composer_ready(terminal.text) and any(
                request.get('method') == 'thread/start' for request in server.requests),
                'private fixture failed before mod acceptance', 25)
            capture(terminal, name + '-startup')
            report['stages'].append(name + ': private startup and editable composer')
            return terminal, home

        try:
            first, first_home = start('original')
            wait(terminals, lambda: 'orbit-panel-1.0.0' in first.text and 'garden-panel-1.0.0' in first.text,
                 'missing native mod rows after successful private startup', 20)
            wait(terminals, lambda: any('broken-panel' in line and 'unavailable' in line
                                       for line in first.screen.display),
                 'malformed package did not produce its isolated unavailable row', 20)
            assert any(call['label'] == 'broken-panel-1.0.0' for call in traces(first_home))
            capture(first, 'two-packages')
            report['stages'].append('one binary loads two differently named packages')
            footer = first.text.split('›')[-1]
            lines = [line.strip() for line in footer.splitlines() if line.strip()]
            mod_lines = [line for line in lines if 'orbit-panel' in line or 'garden-panel' in line]
            assert len(lines) <= 8, lines
            assert len(mod_lines) <= 6, mod_lines
            assert 'orbit-panel-1.0.0 1' in mod_lines[0], mod_lines
            assert 'garden-panel-1.0.0 1' in mod_lines[1], mod_lines
            assert not any('unsupported-panel-1.0.0' in line or 'broken-panel-1.0.0' in line
                           or 'disabled-panel-1.0.0' in line for line in lines), lines
            report['stages'].append('config order, summary fairness, six-row cap, failed/unsupported/disabled isolation')
            os.write(first.master, b'mod-editable-draft')
            wait(terminals, lambda: 'mod-editable-draft' in first.text, 'mod rows blocked typing')
            first.resize(22, 75)
            wait(terminals, lambda: 'mod-editable-draft' in first.text, 'resize lost draft')
            capture(first, 'narrow-draft')
            report['stages'].append('narrow composer remains editable')
            point(mods, 'orbit-panel', '2.0.0')
            newer, newer_home = start('advanced')
            wait(terminals, lambda: 'orbit-panel-2.0.0' in newer.text,
                 'new TUI did not select advanced current version', 20)
            def pinned_calls():
                return sum(call['label'] == 'orbit-panel-1.0.0' for call in traces(first_home))

            previous_calls = pinned_calls()
            wait(terminals, lambda: pinned_calls() > previous_calls,
                 'existing TUI did not make its pinned refresh', 20)
            assert 'orbit-panel-1.0.0' in first.text and 'orbit-panel-2.0.0' not in first.text, first.text
            assert all(call['label'] != 'orbit-panel-2.0.0' for call in traces(first_home))
            capture(first, 'original-still-pinned')
            capture(newer, 'advanced-version')
            report['stages'].append('current advances only for new TUI; live TUI refresh stays pinned')
            point(mods, 'orbit-panel', '1.0.0')
            rollback, rollback_home = start('rollback')
            wait(terminals, lambda: 'orbit-panel-1.0.0' in rollback.text,
                 'new TUI did not select rollback version', 20)
            assert 'orbit-panel-2.0.0' in newer.text, newer.text
            capture(rollback, 'rollback-version')
            assert hashlib.sha256(native.read_bytes()).hexdigest() == report['binary_sha256']
            report['stages'].append('rollback without rebuilding or changing the binary')
            zero, zero_home = start('zero-mods', zero=True)
            settle(1)
            assert not traces(zero_home), 'zero-mod configuration spawned a package'
            assert not any('orbit-panel' in line or 'garden-panel' in line for line in zero.screen.display)
            capture(zero, 'zero-mods')
            report['stages'].append('empty enabled list preserves ordinary frontend')
            for home in homes:
                records = traces(home)
                assert all(record['label'] != 'disabled-panel-1.0.0' for record in records), records
                assert all(record['label'] != 'unsupported-panel-1.0.0' for record in records), records
                assert all(record['request']['session']['thread_id'] == THREAD for record in records), records
            payload_after = {str(path.relative_to(mods)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in mods.rglob('*') if path.is_file() and 'current' not in path.parts}
            assert payload_after == payload_before, 'refresh changed package payloads'
            report.update(passed=True, payload_unchanged=True, supplied_consumers=[str(p) for p in args.package_dir])
        except BaseException as error:
            report['failure'] = str(error)
            raise
        finally:
            for index, terminal in enumerate(terminals):
                capture(terminal, f'last-{index}')
                terminal.close()
            forbidden = {'turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start'}
            forbidden_requests = [request for server in servers for request in server.requests
                                  if request.get('method') in forbidden]
            report['display_model_requests'] = len(forbidden_requests)
            report['server_errors'] = [error for server in servers for error in server.errors]
            for index, server in enumerate(servers):
                (args.evidence / f'rpc-{index}.json').write_text(json.dumps(server.requests, indent=2) + '\n')
                server.close()
            for index, home in enumerate(homes):
                (args.evidence / f'package-calls-{index}.json').write_text(json.dumps(traces(home), indent=2) + '\n')
            report['passed'] &= not forbidden_requests and not report['server_errors']
            (args.evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    assert report['passed'], report
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--node', default=NODE, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--package-dir', action='append', default=[], type=Path,
                        help='additional already-built consumer version to copy into the private root')
    run(parser.parse_args())
