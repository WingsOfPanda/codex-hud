"""Short native-shell topics survive refresh and reconnect; no model requests."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from fixtures.native_server import AppServer
from native_smoke import Terminal, assert_compact_hud, composer_ready, local_shell_smoke, shell_rows, wait


def run(binary, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    terminal = None
    with tempfile.TemporaryDirectory(prefix='plus-topics-') as temporary:
        root = Path(temporary)
        home, work = root / 'home', root / 'work'
        home.mkdir(mode=0o700)
        work.mkdir()
        (home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="openai"\n'
            'suppress_unstable_features_warning=true\nanalytics.enabled=false\n'
            'features.daemon_auto_start=false\nfeatures.plugins=false\n')
        (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
        socket = home / 'app-server-control/app-server-control.sock'
        socket.parent.mkdir(mode=0o700)
        server = AppServer('00000000-0000-0000-0009-000000000001', work, 0, socket_path=socket)
        socket.chmod(0o600)
        commands = [
            '# codex-plus-shell: background\n# codex-plus-task: x86 qualification\n'
            './bin/codex-plus run -- bash -c \'set -e; CARGO_TARGET_DIR="$PWD/.build/detached-jobs/admission/target" cargo +1.99.1 test --locked --manifest-path rust-runtime/Cargo.toml --lib\'',
            '# codex-plus-shell: background\n# codex-plus-task: ARM qualification\n'
            './bin/codex-plus run -- python3 .build/detached-jobs/run-arm-final.py',
        ]
        server.shells = [server.shell(index + 1, command) for index, command in enumerate(commands)]
        try:
            def named():
                rows = shell_rows(terminal.text)
                return (len(rows) == 2 and rows[0].startswith('Background · x86 qualification · seen ')
                        and rows[1].startswith('Background · ARM qualification · seen '))

            terminal = Terminal(binary, home, work)
            wait([terminal], lambda: composer_ready(terminal.text) and len(shell_rows(terminal.text)) == 2,
                 'native background rows did not appear', 25)
            assert named(), terminal.text
            assert 'CARGO_TARGET_DIR' not in terminal.text and '.build/' not in terminal.text
            assert [row['command'] for row in server.shells] == commands, 'display changed raw commands'
            assert_compact_hud(terminal.text, rows=2)
            os.write(terminal.master, b'topic-draft')
            wait([terminal], lambda: 'topic-draft' in terminal.text, 'topic blocked composer')
            replies = terminal.resize(12, 65)
            wait([terminal], lambda: terminal.resized_frame(replies) and named() and 'topic-draft' in terminal.text,
                 'narrow topic rows lost names or draft')
            assert_compact_hud(terminal.text, rows=2)
            (evidence / 'named-narrow.txt').write_text(terminal.text)
            (evidence / 'first.ansi').write_bytes(terminal.output)
            terminal.close()
            terminal = Terminal(binary, home, work)
            wait([terminal], lambda: composer_ready(terminal.text) and named(), 'topics lost on reconnect', 25)
            server.shells = [server.shell(3, 'python3 .build/detached-jobs/run-arm-final.py')]
            wait([terminal], lambda: any(row.startswith('Background · python3 run-arm-final.py · seen ')
                 for row in shell_rows(terminal.text)), 'legacy script fallback kept the path')
            (evidence / 'legacy-script.txt').write_text(terminal.text)
            server.shells = []
            wait([terminal], lambda: not shell_rows(terminal.text), 'completed topic rows remained')
            assert not server.errors, server.errors
            assert not [r for r in server.requests if r['method'] in ('turn/start', 'turn/steer')]
            report = dict(passed=True, binary=str(binary), display_model_requests=0,
                          stages=['named', 'narrow-editable', 'reconnect', 'legacy-script', 'complete'])
        finally:
            if terminal:
                (evidence / 'last-screen.txt').write_text(terminal.text)
                (evidence / 'last.ansi').write_bytes(terminal.output)
                terminal.close()
            server.close()
            (evidence / 'rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')
    actual = evidence / 'actual-native'
    actual.mkdir()
    with tempfile.TemporaryDirectory(prefix='plus-topic-live-') as temporary:
        report['actual_native'] = local_shell_smoke(binary, Path(temporary), actual,
                                                   task_topic='Verify native shell topics')
    (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.evidence.resolve())
